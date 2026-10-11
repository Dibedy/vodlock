import re

from chat_archive import MAX_MESSAGES, MAX_MESSAGES_PER_SECOND, clean_fragment, clean_text, convert_chat


def compact_chat(source, rows):
    source_id = source.get("metadata", {}).get("vod_id") or source["external_id"]
    messages = []
    per_second: dict[int, int] = {}
    for row in rows:
        second = int(row["media_time"])
        if per_second.get(second, 0) >= MAX_MESSAGES_PER_SECOND:
            continue
        value = row["message"]
        if row["origin"] == "vod":
            converted = convert_chat({"comments": [value]}, source_id)["messages"]
            if not converted:
                continue
            message = converted[0]
        else:
            fragments = []
            for fragment in value["message"].get("fragments", []):
                text = clean_fragment(fragment.get("text"), 500)
                if not text:
                    continue
                emote = str((fragment.get("emote") or {}).get("id", ""))
                fragments.append([text, emote] if re.fullmatch(r"[0-9]{1,20}", emote) else [text])
            if not fragments:
                fragments = [[clean_text(value["message"].get("text"), 500)]]
            color = value.get("color", "")
            message = {
                "t": round(row["media_time"], 2),
                "u": clean_text(value["user"], 40),
                "c": color if re.fullmatch(r"#[0-9A-Fa-f]{6}", color) else "",
                "f": fragments[:50],
            }
        messages.append(message)
        per_second[second] = per_second.get(second, 0) + 1
        if len(messages) >= MAX_MESSAGES:
            break
    return {"v": 1, "source": source_id, "messages": messages}


def chat_alignment(mapping, source_id):
    scale = mapping["timelineScale"]
    return {
        "source": "twitch:" + source_id,
        "timelineScale": 1 / scale,
        "strictCoverage": True,
        "segments": [
            {
                "offset": -section["offset"] / scale,
                "targetStart": max(0, section["canonicalStart"]),
                "targetEnd": section["canonicalEnd"],
            }
            for section in mapping["segments"]
            if section["canonicalEnd"] > max(0, section["canonicalStart"])
        ],
    }
