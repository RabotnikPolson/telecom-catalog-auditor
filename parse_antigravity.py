import json
import re
from pathlib import Path

# Прямой абсолютный путь к логам сессии в Antigravity IDE
INPUT_FILE = Path(
    r"C:\Users\Дамир\.gemini\antigravity-ide\brain\dd611a98-2475-4790-ade8-2ea7093cca49\.system_generated\logs\transcript_full.jsonl"
)
OUTPUT_FILE = Path(
    r"C:\Users\Дамир\.gemini\antigravity-ide\scratch\telecom_catalog_auditor\antigravity_history.md"
)

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<\/?USER_REQUEST>", "", text)
    text = re.sub(r"<ADDITIONAL_METADATA>[\s\S]*?<\/ADDITIONAL_METADATA>", "", text)
    text = re.sub(r"<USER_SETTINGS_CHANGE>[\s\S]*?<\/USER_SETTINGS_CHANGE>", "", text)
    return text.strip()

def process_chat_log():
    if not INPUT_FILE.exists():
        # Если в указанной папке нет, проверяем текущую директорию скрипта
        fallback = Path("transcript_full.jsonl")
        if fallback.exists():
            target_input = fallback
        else:
            print(f"Файл не найден по пути: {INPUT_FILE}")
            print("Скопируйте transcript_full.jsonl в папку со скриптом или укажите актуальный путь к сессии.")
            return
    else:
        target_input = INPUT_FILE

    print(f"Чтение файла: {target_input}")
    with open(target_input, "r", encoding="utf-8") as f_in, open(OUTPUT_FILE, "w", encoding="utf-8") as f_out:
        f_out.write("# История сессии: Telecom Catalog Auditor (Antigravity IDE)\n\n---\n\n")

        count = 0
        for line in f_in:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            source = row.get("source")
            step_type = row.get("type")
            created_at = row.get("created_at", "")
            raw_content = row.get("content", "")

            if source == "USER_EXPLICIT" and step_type == "USER_INPUT":
                text = clean_text(raw_content)
                if text:
                    f_out.write(f"### 👤 Дамир *({created_at})*\n\n")
                    f_out.write(f"{text}\n\n---\n\n")
                    count += 1

            elif source == "MODEL" and step_type == "PLANNER_RESPONSE":
                text = clean_text(raw_content)
                if text:
                    f_out.write(f"### 🤖 Ассистент (Lead Python Dev) *({created_at})*\n\n")
                    f_out.write(f"{text}\n\n---\n\n")
                    count += 1

    print(f"Готово! Сохранено сообщений: {count}")
    print(f"Результат записан в: {OUTPUT_FILE}")

if __name__ == "__main__":
    process_chat_log()
