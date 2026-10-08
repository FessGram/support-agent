import csv
import json
import logging
import re
import time
from datetime import datetime

import requests
from openai import OpenAI

CONFIG_PATH = "config.json"

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    cfg = json.load(f)

TG_API = f"https://api.telegram.org/bot{cfg['telegram_token']}"
client = OpenAI(base_url=cfg["ollama_url"], api_key="ollama")  # Ollama ключ не проверяет

SYSTEM_PROMPT = """Ты — ассистент первой линии ИТ-поддержки компании.
Проанализируй обращение пользователя и верни СТРОГО валидный JSON без какого-либо
текста до или после:
{
  "category": "одна из: Сеть, Почта, Принтеры, 1С/ERP, Доступ и учетки, Оборудование, Прочее",
  "priority": "одна из: Критичный, Высокий, Средний, Низкий",
  "user_reply": "вежливый ответ пользователю: 1-3 конкретных шага, что сделать прямо сейчас",
  "admin_note": "что проверить системному администратору, 1-2 пункта"
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


def extract_json(text):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"LLM вернул не JSON: {text[:200]}")
    return json.loads(m.group(0))


def analyze(text):
    resp = client.chat.completions.create(
        model=cfg["model"],
        temperature=0.2,  # низкая = стабильные классификации
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    )
    return extract_json(resp.choices[0].message.content or "")


def save_ticket(source, data):
    # utf-8-sig — чтобы Excel открывал кириллицу без танцев
    with open("tickets.csv", "a", newline="", encoding="utf-8-sig") as f:
        csv.writer(f, delimiter=";").writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            data.get("category"), data.get("priority"),
            source.replace("\n", " ")[:100],
            data.get("admin_note"),
        ])


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

    tg("sendMessage", chat_id=chat_id, text=(
        "🎫 Категория: " + str(data.get("category")) +
        "\n⚡️ Приоритет: " + str(data.get("priority")) +
        "\n\nЧто делать:\n" + str(data.get("user_reply")) +
        "\n\nЕсли не помогло — передал заявку ИТ-отделу."
    ))
    tg("sendMessage", chat_id=cfg["admin_chat_id"], text=(
        "📥 Заявка [" + str(data.get("priority")) + "] " + str(data.get("category")) +
        "\nОбращение: " + text[:300] +
        "\nПроверить: " + str(data.get("admin_note"))
    ))
    save_ticket(text, data)
    logging.info("Обработано: %s / %s", data.get("category"), data.get("priority"))


def main():
    offset = 0
    tg("sendMessage", chat_id=cfg["admin_chat_id"], text="🤖 Support-агент запущен")
    while True:
        updates = tg("getUpdates", offset=offset, timeout=30)  # long polling
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message")
            if msg and "text" in msg:
                handle(msg["text"], msg["chat"]["id"])


if __name__ == "__main__":
    main()