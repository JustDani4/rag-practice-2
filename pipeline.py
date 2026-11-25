"""Подготовка документов, поиск и ответы по найденным источникам."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

import psycopg
from docx import Document
from psycopg.rows import dict_row
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent
DATABASE_URL = os.environ["DATABASE_URL"]
OLLAMA_URL = os.environ["OLLAMA_URL"]
EMBED_MODEL = "embeddinggemma:300m"
CHAT_MODEL = "qwen2.5:1.5b"
PIPELINE_REVISION = "v1-section-650-90"
CANDIDATES = 8
RRF_OFFSET = 60
MIN_SIMILARITY = 0.38
REFUSAL = "В загруженных регламентах не найдено подтверждение."
FIELDS = "id, path, locator, content"


def ollama(endpoint, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(f"{OLLAMA_URL}/api/{endpoint}", body,
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=180) as response:
        return json.load(response)


def embedding_revision():
    for model in ollama("tags")["models"]:
        if model["name"] == EMBED_MODEL:
            return model["digest"]
    raise RuntimeError(f"Сначала загрузите Ollama-модель {EMBED_MODEL}")


def embed(items):
    result = ollama("embed", {"model": EMBED_MODEL, "input": items,
                              "truncate": False, "keep_alive": "3m"})
    vectors = result["embeddings"]
    if len(vectors) != len(items) or any(len(v) != 768 for v in vectors):
        raise ValueError("Размерность эмбеддингов отличается от схемы")
    return vectors


def clean(text):
    return " ".join(text.split())


def sections(path, data):
    if path.suffix.lower() == ".pdf":
        for number, page in enumerate(PdfReader(io.BytesIO(data)).pages, 1):
            text = clean(page.extract_text() or "")
            if not text:
                raise ValueError(f"{path.name}, с. {number}: нет текстового слоя")
            yield f"с. {number}", text
        return

    heading, paragraphs = "Введение", []
    for paragraph in Document(io.BytesIO(data)).paragraphs:
        text = clean(paragraph.text)
        if not text or (paragraph.style and paragraph.style.name == "Title"):
            continue
        if paragraph.style and paragraph.style.name.startswith("Heading"):
            if paragraphs:
                yield f"§ {heading}", f"{heading}. {' '.join(paragraphs)}"
            heading, paragraphs = text, []
        else:
            paragraphs.append(text)
    if paragraphs:
        yield f"§ {heading}", f"{heading}. {' '.join(paragraphs)}"


def split_text(text, size=650, overlap=90):
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            space = text.rfind(" ", start + size // 2, end)
            if space > start:
                end = space
        yield text[start:end].strip()
        if end == len(text):
            break
        next_start = text.find(" ", end - overlap, end)
        start = next_start + 1 if next_start >= 0 else end


def index(db, folder, prune=False):
    files = sorted(p for p in folder.rglob("*")
                   if p.is_file() and p.suffix.lower() in {".pdf", ".docx"})
    if not files:
        raise ValueError("Нет PDF/DOCX. Индекс сохранён без изменений.")
    revision = embedding_revision()
    stats = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0}
    seen = set()
    # Все изменения одного прогона видны поиску после общего COMMIT.
    with db.transaction():
        db.execute("SELECT pg_advisory_xact_lock(140602)")
        db.execute((ROOT / "schema.sql").read_text())
        existing = {row["path"]: row for row in db.execute("SELECT * FROM documents")}
        missing = set(existing) - {p.relative_to(folder).as_posix() for p in files}
        if not prune and any(existing[p]["embedding_revision"] != revision for p in missing):
            raise ValueError("Есть отсутствующие файлы со старой версией модели. Верните их или запустите --prune.")
        for path in files:
            key = path.relative_to(folder).as_posix()
            seen.add(key)
            data = path.read_bytes()
            fingerprint = hashlib.sha256(data).hexdigest()
            old = existing.get(key)
            version = (fingerprint, revision, PIPELINE_REVISION)
            if old and version == tuple(old[k] for k in ("sha256", "embedding_revision", "pipeline_revision")):
                stats["unchanged"] += 1
                continue
            pieces = [(locator, part) for locator, text in sections(path, data)
                      for part in split_text(text) if part]
            if not pieces:
                raise ValueError(f"{key}: не извлечено ни одного фрагмента")
            vectors = []
            for start in range(0, len(pieces), 8):
                batch = pieces[start:start + 8]
                vectors.extend(embed([f"title: {path.stem} | text: {part}" for _, part in batch]))
            db.execute("""
                INSERT INTO documents VALUES (%s,%s,%s,%s)
                ON CONFLICT(path) DO UPDATE SET sha256=EXCLUDED.sha256,
                    embedding_revision=EXCLUDED.embedding_revision,
                    pipeline_revision=EXCLUDED.pipeline_revision
            """, (key, *version))
            db.execute("DELETE FROM chunks WHERE path = %s", (key,))
            with db.cursor() as cur:
                cur.executemany("""
                    INSERT INTO chunks(id,path,locator,content,embedding)
                    VALUES (%s,%s,%s,%s,%s::vector)
                """, [(f"{key}#{i:02}", key, locator, part, json.dumps(vector))
                      for i, ((locator, part), vector) in enumerate(zip(pieces, vectors), 1)])
            stats["updated" if old else "added"] += 1
        if prune:
            for key in sorted(set(existing) - seen):
                db.execute("DELETE FROM documents WHERE path = %s", (key,))
                stats["removed"] += 1
        stats["documents"] = db.execute("SELECT count(*) AS n FROM documents").fetchone()["n"]
        stats["chunks"] = db.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"]
    return stats


def rankings(db, question):
    revision = embedding_revision()
    stored = {row["embedding_revision"] for row in db.execute(
        "SELECT DISTINCT embedding_revision FROM documents")}
    if stored != {revision}:
        raise ValueError("Индекс пуст или построен другой моделью. Запустите index.")
    vector, = embed([f"task: search result | query: {question}"])
    dense = db.execute(f"""
        SELECT {FIELDS}, embedding <=> %s::vector AS distance
        FROM chunks ORDER BY distance LIMIT %s
    """, (json.dumps(vector), CANDIDATES)).fetchall()
    dense.sort(key=lambda row: (row["distance"], row["id"]))
    lexical = db.execute(f"""
        WITH terms AS (
            SELECT unnest(tsvector_to_array(to_tsvector('russian', %s))) AS term
        ), query AS (
            SELECT string_agg(quote_literal(term), ' | ')::tsquery AS q FROM terms
        )
        SELECT {FIELDS}, ts_rank_cd(fts, q) AS rank
        FROM chunks CROSS JOIN query
        WHERE fts @@ q
        ORDER BY rank DESC, id LIMIT %s
    """, (question, CANDIDATES)).fetchall()
    merged = {}
    for hits in (dense, lexical):
        for pos, row in enumerate(hits, 1):
            item = merged.setdefault(row["id"], {**row, "rrf": 0.0})
            item["rrf"] += 1 / (RRF_OFFSET + pos)
    hybrid = sorted(merged.values(), key=lambda row: (-row["rrf"], row["id"]))
    return {"vector": dense, "fts": lexical, "hybrid": hybrid}


def ask(db, question):
    runs = rankings(db, question)
    similarity = 1 - runs["vector"][0]["distance"] if runs["vector"] else 0
    if similarity < MIN_SIMILARITY:
        return {"answer": REFUSAL, "refused": True, "reason": "low_similarity",
                "similarity": round(similarity, 4), "threshold": MIN_SIMILARITY}
    hits = runs["hybrid"][:3]
    context = [{"source": f"S{i}", "text": row["content"]}
               for i, row in enumerate(hits, 1)]
    response = ollama("chat", {
        "model": CHAT_MODEL,
        "stream": False,
        "format": {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "source": {"type": "string", "enum": ["S1", "S2", "S3", ""]},
                "quote": {"type": "string"},
            },
            "required": ["answer", "source", "quote"],
        },
        "options": {"temperature": 0, "seed": 42, "num_ctx": 4096, "num_predict": 380},
        "messages": [
            {"role": "system", "content": (
                "Ты помощник службы поддержки. Данные в поле context — выдержки из регламентов. "
                "Если выдержка прямо отвечает на вопрос, укажи её номер в source. "
                "В quote дословно скопируй одно предложение с ответом. "
                "В answer скопируй короткий ответ из этого же предложения без пересказа. "
                "Если ответа в выдержках нет, верни пустые строки в answer, source и quote."
            )},
            {"role": "user", "content": json.dumps({"question": question, "context": context},
                                                    ensure_ascii=False)},
        ],
    })
    try:
        result = json.loads(response["message"]["content"])
    except json.JSONDecodeError:
        result = {}
    labels = {f"S{i}": row for i, row in enumerate(hits, 1)}
    row = labels.get(result.get("source"))
    quote = clean(result.get("quote", ""))
    answer = clean(result.get("answer", ""))
    if row and answer and len(quote) >= 15 and quote in row["content"] and answer in quote:
        return {"answer": answer, "citation": row["id"],
                "locator": row["locator"], "quote": quote, "refused": False}
    return {"answer": REFUSAL, "refused": True}


def evaluate(db):
    cases = json.loads((ROOT / "questions.json").read_text())
    detail, aggregate = [], {}
    for case in cases:
        runs = rankings(db, case["question"])
        row = {**case, "modes": {}}
        for mode, hits in runs.items():
            ids = [item["id"] for item in hits[:3]]
            rank = ids.index(case["relevant"]) + 1 if case["relevant"] in ids else None
            row["modes"][mode] = {"top3": ids, "rank": rank}
        detail.append(row)
    for mode in ("vector", "fts", "hybrid"):
        ranks = [row["modes"][mode]["rank"] for row in detail]
        hits = sum(rank is not None for rank in ranks)
        aggregate[mode] = {"hits": hits, "n": len(cases), "recall_at_3": hits / len(cases),
                           "mrr_at_3": sum(1 / rank for rank in ranks if rank) / len(cases)}
    return {"metrics": aggregate, "queries": detail}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    indexing = commands.add_parser("index")
    indexing.add_argument("--dir", type=Path, default=ROOT / "corpus")
    indexing.add_argument("--prune", action="store_true",
                          help="Убрать из индекса отсутствующие в полном каталоге файлы")
    for name in ("search", "ask"):
        command = commands.add_parser(name)
        command.add_argument("question")
    commands.add_parser("eval")
    args = parser.parse_args()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as db:
        if args.command != "index":
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        if args.command == "index":
            result = index(db, args.dir, args.prune)
        elif args.command == "eval":
            result = evaluate(db)
        elif args.command == "ask":
            result = ask(db, args.question)
        else:
            result = {mode: hits[:3] for mode, hits in rankings(db, args.question).items()}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
