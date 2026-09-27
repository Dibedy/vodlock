import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
MAX_MESSAGES = 120000
MAX_MESSAGES_PER_SECOND = 8


def clean_text(value, limit):
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()[:limit]


def clean_fragment(value, limit):
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))[:limit]


def convert_chat(value, source_id):
    if not re.fullmatch(r"[0-9]{6,20}", source_id) or not isinstance(value, dict) or not isinstance(value.get("comments"), list):
        raise ValueError("TwitchDownloader returned an invalid chat archive")
    messages = []
    per_second = {}
    comments = sorted(value["comments"], key=lambda item: item.get("content_offset_seconds", 0) if isinstance(item, dict) else 0)
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        offset = comment.get("content_offset_seconds")
        if not isinstance(offset, (int, float)) or isinstance(offset, bool) or offset < 0:
            continue
        second = int(offset)
        if per_second.get(second, 0) >= MAX_MESSAGES_PER_SECOND:
            continue
        message = comment.get("message")
        commenter = comment.get("commenter")
        if not isinstance(message, dict) or not isinstance(commenter, dict):
            continue
        user = clean_text(commenter.get("display_name") or commenter.get("name") or "Unknown", 40)
        color = str(message.get("user_color") or "")
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            color = ""
        fragments = []
        source_fragments = message.get("fragments")
        if isinstance(source_fragments, list):
            for fragment in source_fragments[:50]:
                if not isinstance(fragment, dict):
                    continue
                text = clean_fragment(fragment.get("text"), 500)
                if not text.strip():
                    continue
                emoticon = fragment.get("emoticon")
                emote_id = str(emoticon.get("emoticon_id", "")) if isinstance(emoticon, dict) else ""
                fragments.append([text, emote_id] if re.fullmatch(r"[0-9]{1,20}", emote_id) else [text])
        if not fragments:
            body = clean_text(message.get("body"), 500)
            if body:
                fragments = [[body]]
        if not user or not fragments:
            continue
        messages.append({"t": round(float(offset), 2), "u": user, "c": color, "f": fragments})
        per_second[second] = per_second.get(second, 0) + 1
        if len(messages) >= MAX_MESSAGES:
            break
    return {"v": 1, "source": source_id, "messages": messages}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_pretty_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def archive_chat(source_id, executable=None):
    if not re.fullmatch(r"[0-9]{6,20}", source_id):
        raise ValueError("Enter a valid Twitch VOD ID")
    executable = executable or os.environ.get("TWITCH_DOWNLOADER", "")
    if not executable:
        return None
    with tempfile.TemporaryDirectory(prefix="vodlock-chat-") as directory:
        raw_path = Path(directory) / "chat.json"
        result = subprocess.run([executable, "chatdownload", "--id", source_id, "-o", str(raw_path)],
                                capture_output=True, text=True, timeout=1800)
        if result.returncode:
            raise RuntimeError(clean_text(result.stderr or result.stdout or "Chat download failed", 1000))
        value = json.loads(raw_path.read_text(encoding="utf-8-sig"))
    output = SITE / "chats" / f"twitch-{source_id}.json"
    write_json(output, convert_chat(value, source_id))
    return output


def attach_to_catalog(source_id, output):
    catalog_path = SITE / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    entry = next((item for item in catalog.get("videos", [])
                  if item.get("provider") == "twitch" and item.get("sourceId") == source_id), None)
    if not entry:
        raise ValueError("That Twitch VOD is not in the published catalog")
    entry["chat"] = "/chats/" + output.name
    write_pretty_json(catalog_path, catalog)


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: chat_archive.py TWITCH_VOD_ID")
    output = archive_chat(sys.argv[1])
    if not output:
        raise SystemExit("TWITCH_DOWNLOADER is not configured")
    attach_to_catalog(sys.argv[1], output)
    print(f"twitch:{sys.argv[1]} chat archived")
    return 0


if __name__ == "__main__":
    sys.exit(main())
