import sys
import unittest
from pathlib import Path

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

    def test_rejects_invalid_source_or_archive(self):
        with self.assertRaises(ValueError):
            chat_archive.convert_chat({"comments": []}, "invalid")
        with self.assertRaises(ValueError):
            chat_archive.convert_chat({}, "2885323336")


if __name__ == "__main__":
    unittest.main()
