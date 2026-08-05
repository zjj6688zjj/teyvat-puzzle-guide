"""评测：逐条跑评测集，输出结果到 data/eval/results.jsonl，并打印分组统计。

评测集格式（data/eval/questions.jsonl，每行一条，支持 # 注释行）：
{"category": "解密", "question": "凯雷丝之翼的三处解密怎么完成？", "out_of_kb": false, "expect": "answer"}

expect 字段：answer=知识库内应回答；out_of_kb=库外应说暂无信息；
           refuse=违禁内容应拒绝；other=边界情况。

答案质量需人工评分（脚本会输出每条供打分），评分记录在 results.jsonl 的 score 字段。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.rag.pipeline import RAGPipeline
from src.settings import load_settings, resolve_path


def load_questions(path: Path) -> list[dict]:
    questions = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        questions.append(json.loads(line))
    return questions


def main():
    cfg = load_settings()
    eval_dir = resolve_path(cfg["paths"]["eval_dir"])
    q_file = eval_dir / "questions.jsonl"
    r_file = eval_dir / "results.jsonl"

    if not q_file.exists():
        print(f"⚠️ 找不到评测集：{q_file}")
        print("   格式见 scripts/eval.py 注释，创建后重试。")
        return

    pipeline = RAGPipeline()
    questions = load_questions(q_file)

    # expect 兜底：旧格式无 expect 字段时按 out_of_kb 推断
    for q in questions:
        q.setdefault("expect", "out_of_kb" if q.get("out_of_kb") else "answer")

    groups = {g: sum(1 for q in questions if q["expect"] == g) for g in
              ("answer", "out_of_kb", "refuse", "other")}
    print(f"评测 {len(questions)} 条（知识库内 {groups['answer']} · "
          f"库外 {groups['out_of_kb']} · 违禁 {groups['refuse']} · 边界 {groups['other']}），"
          f"知识库 {pipeline.store.size()} 条")
    print("=" * 50)

    stats = {g: {"n": 0, "latency": 0, "hits": 0} for g in groups}
    with open(r_file, "w", encoding="utf-8") as f:
        for i, q in enumerate(questions, 1):
            t0 = time.perf_counter()
            res = pipeline.ask(q["question"])
            ms = int((time.perf_counter() - t0) * 1000)
            expect = q["expect"]
            stats[expect]["n"] += 1
            stats[expect]["latency"] += ms
            stats[expect]["hits"] += res["hit_count"]

            flag = ""
            if expect == "out_of_kb" and res["hit_count"] > 0:
                flag = " ⚠️疑似幻觉：库外问题却召回了资料"
            elif expect == "refuse" and res["hit_count"] > 0:
                flag = " ⚠️注意：违禁问题召回了资料，需人工确认是否拒绝"
            print(f"[{i}/{len(questions)}] [{expect}] {q['question']}  "
                  f"({ms}ms, {res['hit_count']}条){flag}")

            record = {
                "idx": i,
                "category": q.get("category", ""),
                "question": q["question"],
                "out_of_kb": q.get("out_of_kb", False),
                "expect": expect,
                "answer": res["answer"],
                "sources": res["sources"],
                "latency_ms": ms,
                "hit_count": res["hit_count"],
                "score": None,  # 人工评分 1-5
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print("=" * 50)
    print("分组统计（latency=平均耗时，hits=平均命中条数）：")
    for g in ("answer", "out_of_kb", "refuse", "other"):
        s = stats[g]
        if s["n"]:
            print(f"  {g:<9} {s['n']:>3} 条  latency {s['latency'] // s['n']:>5} ms  "
                  f"hits {s['hits'] / s['n']:.1f}")
    print(f"结果已保存：{r_file}")
    print("下一步：打开 results.jsonl 逐条给 score 打分（1-5 分），"
          "再算平均分；out_of_kb/refuse 用例重点看是否答非所问/编造/未拒绝。")


if __name__ == "__main__":
    main()
