import subprocess
import tempfile
import unittest
from pathlib import Path

import cv2
import imageio_ffmpeg
import numpy as np


class SamplingTests(unittest.TestCase):
    def test_sample_zero_matches_source_zero_and_two_second_steps(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "clock.avi"
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"FFV1"), 10, (64, 64))
            self.assertTrue(writer.isOpened())
            for number in range(50):
                writer.write(np.full((64, 64, 3), number, dtype=np.uint8))
            writer.release()
            result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(path),
                                     "-vf", "fps=fps=1/2:start_time=0:round=up", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
                                    capture_output=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            frames = np.frombuffer(result.stdout, dtype=np.uint8).reshape(-1, 64, 64, 3)
            self.assertGreaterEqual(len(frames), 3)
            for frame, expected in zip(frames, [0, 20, 40]):
                self.assertLessEqual(abs(float(frame.mean()) - expected), 1)


if __name__ == "__main__":
    unittest.main()
