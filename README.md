# Практическая № 2 · ETL для RAG

Воднев Даниил Евгеньевич, БИВТ-24-АЭ-3. Вариант 3 — поиск по документам.

- [Отчёт](отчет.md)
- [Схема Draw.io](architecture.drawio) · [Просмотр PNG](architecture.png)
- [Результаты проверки поиска](evidence/retrieval.json)

## Запуск

Нужен Docker с Compose. При первом запуске Ollama скачает две модели суммарным объёмом около 1,6 ГБ. Сервисы доступны внутри сети проекта.

```bash
docker compose up -d --build --wait
docker compose exec -T ollama ollama pull embeddinggemma:300m
docker compose exec -T ollama ollama pull qwen2.5:1.5b

docker compose exec -T rag python pipeline.py index
docker compose exec -T rag python pipeline.py search 'PAY-502'
docker compose exec -T rag python pipeline.py ask 'Сколько дней посылка хранится в пункте выдачи?'
docker compose exec -T rag python pipeline.py eval
docker compose ps
```

Команда `index` сканирует `corpus/`, пропускает неизменённые документы и заменяет фрагменты изменённых. `index --prune` сверяет индекс с полным каталогом и удаляет из индекса записи отсутствующих документов. Файлы доступны контейнеру только для чтения.

После работы `docker compose down` остановит контейнеры. База и модели останутся в именованных томах.

Документы в `corpus/` вымышлены и созданы для демонстрации. Тексты регламентов, коды и сроки относятся к этому набору.
