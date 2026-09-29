import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "indexer"))
import chat_archive


class ChatArchiveTests(unittest.TestCase):
    def test_converts_chat_to_compact_safe_messages(self):
        source = {"comments": [
            {"content_offset_seconds": 12.345, "commenter": {"display_name": "Viewer"},
             "message": {"user_color": "#12Ab34", "fragments": [
                 {"text": "hello ", "emoticon": None},
                 {"text": "Kappa", "emoticon": {"emoticon_id": "25"}}
             ]}},
            {"content_offset_seconds": 13, "commenter": {"name": "Second"},
             "message": {"user_color": "bad", "body": "line\nbreak"}}
        ]}
        result = chat_archive.convert_chat(source, "2885323336")
        self.assertEqual(result["source"], "2885323336")
        self.assertEqual(result["messages"][0], {"t": 12.35, "u": "Viewer", "c": "#12Ab34",
                                                 "f": [["hello "], ["Kappa", "25"]]})
        self.assertEqual(result["messages"][1]["f"], [["line break"]])
        self.assertEqual(result["messages"][1]["c"], "")

    def test_limits_dense_chat_per_second(self):
        comments = [{"content_offset_seconds": 5.1 + number / 100,
                     "commenter": {"display_name": f"Viewer{number}"},
                     "message": {"user_color": "", "body": "message"}} for number in range(20)]
        result = chat_archive.convert_chat({"comments": comments}, "2885323336")
        self.assertEqual(len(result["messages"]), chat_archive.MAX_MESSAGES_PER_SECOND)

    def test_converts_embedded_seventv_emotes_without_persisting_images(self):
        emote_id = "01F6M2T8P00000000000000000"
        source = {
            "embeddedData": {"thirdParty": [
                {"id": emote_id, "name": "KEKW", "isZeroWidth": False, "data": "large-embedded-image"},
                {"id": "01F6M2T8P00000000000000001", "name": "overlay", "isZeroWidth": True}
            ]},
            "comments": [{"content_offset_seconds": 4, "commenter": {"display_name": "Viewer"},
                          "message": {"user_color": "", "body": "hello KEKW KEKWish overlay"}}]
        }
        result = chat_archive.convert_chat(source, "2885323336")
        self.assertEqual(result["messages"][0]["f"],
                         [["hello "], ["KEKW", "7tv:" + emote_id], [" KEKWish overlay"]])
        self.assertNotIn("embeddedData", result)

    def test_archive_requests_only_seventv_third_party_emotes(self):
        source = {"comments": []}
        command = []

        def run(args, **kwargs):
            command.extend(args)
            Path(args[args.index("-o") + 1]).write_text(json.dumps(source), encoding="utf-8")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(chat_archive, "SITE", Path(directory)), \
                patch.object(chat_archive.subprocess, "run", side_effect=run):
            chat_archive.archive_chat("2885323336", "TwitchDownloaderCLI")
        self.assertIn("--embed-images", command)
        self.assertIn("--bttv=false", command)
        self.assertIn("--ffz=false", command)
        self.assertIn("--stv=true", command)

    def test_rejects_invalid_source_or_archive(self):
        with self.assertRaises(ValueError):
            chat_archive.convert_chat({"comments": []}, "invalid")
        with self.assertRaises(ValueError):
            chat_archive.convert_chat({}, "2885323336")


if __name__ == "__main__":
    unittest.main()
