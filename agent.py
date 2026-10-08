import csv
import json
import logging
import re
import time
from datetime import datetime

import requests
from openai import OpenAI

CONFIG_PATH = "config.json"
TICKETS_PATH = "tickets.csv"
SIMILAR_LIMIT = 3      # сколько похожих заявок подмешивать в промпт
MIN_SIMILARITY = 0.15  # порог похожести, ниже — случай не считается похожим

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    cfg = json.load(f)

TG_API = f"https://api.telegram.org/bot{cfg['telegram_token']}"
client = OpenAI(base_url=cfg["ollama_url"], api_key="ollama")

SYSTEM_PROMPT = """Ты — ассистент первой линии ИТ-поддержки компании.
Проанализируй обращение пользователя и верни СТРОГО валидный JSON без какого-либо
текста до или после:
{
  "category": "одна из: Сеть, Почта, Принтеры, 1С/ERP, Доступ и учетки, Оборудование, Прочее",
  "priority": "одна из: Критичный, Высокий, Средний, Низкий",
  "user_reply": "вежливый ответ пользователю: 1-3 конкретных шага, что сделать прямо сейчас",
  "admin_note": "что проверить системному администратору, 1-3 пункта"
}"""

logging.basicConfig(filename="agent.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")


def tg(method, **params):
    try:
        r = requests.post(f"{TG_API}/{method}", json=params, timeout=40)
        return r.json().get("result", [])
    except requests.RequestException as e:
        logging.error("Telegram API: %s", e)
        return []


def load_tickets():
    try:
        with open(TICKETS_PATH, "r", newline="", encoding="utf-8-sig") as f:
            return list(csv.reader(f, delimiter=";"))[1:]  # без заголовка
    except FileNotFoundError:
        return []


STOP_WORDS = {"и", "в", "на", "не", "с", "по", "для", "что", "как", "это", "у", "о", "же"}


def similarity(text_a, text_b):
    """Доля общих значимых слов (коэффициент Жаккара), от 0 до 1."""
    wa = set(re.findall(r"[а-яa-z0-9]+", text_a.lower())) - STOP_WORDS
    wb = set(re.findall(r"[а-яa-z0-9]+", text_b.lower())) - STOP_WORDS
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def find_similar(text):
    scored = []
    for row in load_tickets():
        if len(row) < 5:
            continue
        source, category, note = row[3], row[1], row[4]
        score = similarity(text, source)
        if score >= MIN_SIMILARITY:
            scored.append((score, source, category, note))
    scored.sort(key=lambda x: -x[0])
    return scored[:SIMILAR_LIMIT]


def build_messages(text):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    similar = find_similar(text)
    logging.info("Найдено похожих заявок: %d", len(similar))
    if similar:
        cases = "\n\n".join(
            f"Обращение: «{src[:120]}»\nКатегория: {cat}. Решение админа: {note}"
            for _, src, cat, note in similar
        )
        messages.append({
            "role": "system",
            "content": "Похожие случаи из прошлого опыта. Используй их как ориентир "
                       "по категории, приоритету и решению:\n\n" + cases,
        })
    messages.append({"role": "user", "content": text})
    return messages


def extract_json(text):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"LLM вернул не JSON: {text[:200]}")
    return json.loads(m.group(0))


def analyze(text):
    resp = client.chat.completions.create(
        model=cfg["model"],
        temperature=0.2,
        messages=build_messages(text),
    )
    return extract_json(resp.choices[0].message.content or "")


def save_ticket(source, data):
    with open(TICKETS_PATH, "a", newline="", encoding="utf-8-sig") as f:
        csv.writer(f, delimiter=";").writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            data.get("category"), data.get("priority"),
            source.replace("\n", " ")[:100],
            data.get("admin_note"),
        ])


def stats_text():
    rows = load_tickets()
    if not rows:
        return "📊 Журнал заявок пуст"
    counts = {}
    for row in rows:
        cat = row[1] if len(row) > 1 and row[1] else "?"
        counts[cat] = counts.get(cat, 0) + 1
    lines = [f"• {cat}: {n}" for cat, n in sorted(counts.items(), key=lambda x: -x[1])]
    return "📊 Заявки по категориям:\n" + "\n".join(lines)


def handle(text, chat_id):
    logging.info("Обращение от chat_id=%s", chat_id)
    try:
        data = analyze(text)
    except Exception as e:
        logging.error("LLM: %s", e)
        tg("sendMessage", chat_id=chat_id,
           text="⚠️ Заявку принял, но обработать не смог. Передано администратору.")
        tg("sendMessage", chat_id=cfg["admin_chat_id"],
           text=f"⚠️ Ошибка агента: {e}\n\nОбращение: {text[:500]}")
        return

    priority = str(data.get("priority", ""))
    category = str(data.get("category", ""))
    is_critical = priority == "Критичный"

    tg("sendMessage", chat_id=chat_id, text=(
        "🎫 Категория: " + category +
        "\n⚡️ Приоритет: " + priority +
        "\n\nЧто делать:\n" + str(data.get("user_reply")) +
        "\n\nЕсли не помогло — передал заявку ИТ-отделу."
    ))

    icon = "🚨 КРИТИЧНАЯ ЗАЯВКА" if is_critical else "📥 Заявка"
    tg("sendMessage", chat_id=cfg["admin_chat_id"], text=(
        f"{icon} [{priority}] {category}\n"
        f"Обращение: {text[:300]}\n"
        f"Проверить: {data.get('admin_note')}"
    ))
    # второй пуш, чтобы критичное нельзя было пропустить (отключается в конфиге)
    if is_critical and cfg.get("critical_double_alert", True):
        tg("sendMessage", chat_id=cfg["admin_chat_id"],
           text="🚨 Требует реакции сейчас: проверь уведомления.")

    save_ticket(text, data)
    logging.info("Обработано: %s / %s", category, priority)


def main():
    offset = 0
    tg("sendMessage", chat_id=cfg["admin_chat_id"],
       text="🤖 Support-агент v2 запущен (память: вкл, /stats доступна)")
    while True:
        updates = tg("getUpdates", offset=offset, timeout=30)
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message")
            if not msg or "text" not in msg:
                continue
            text, chat_id = msg["text"], msg["chat"]["id"]

            if text.startswith("/start"):
                tg("sendMessage", chat_id=chat_id,
                   text="Опиши проблему — подскажу первые шаги и передам ИТ-отделу.")
            elif text.startswith("/stats") and str(chat_id) == str(cfg["admin_chat_id"]):
                tg("sendMessage", chat_id=chat_id, text=stats_text())
            else:
                handle(text, chat_id)


if __name__ == "__main__":
    main()