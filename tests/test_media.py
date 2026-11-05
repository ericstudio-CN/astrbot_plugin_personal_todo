import base64
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_personal_todo.media import MediaError, image_data_uri

PNG = b"\x89PNG\r\n\x1a\n" + b"test-attachment"


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_is_limited_without_trusting_header(self):
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=PNG * 100))
        async with httpx.AsyncClient(transport=transport) as client:
            with self.assertRaises(MediaError):
                await image_data_uri("https://example.test/image", client, limit=100)

    async def test_actual_image_format_used_instead_of_declared_mime(self):
        transport = httpx.MockTransport(
            lambda _: httpx.Response(200, content=PNG, headers={"content-type": "text/plain"})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            actual = await image_data_uri("https://example.test/image", client, limit=100)
        self.assertEqual(actual, "data:image/png;base64," + base64.b64encode(PNG).decode())

    async def test_failed_download_and_non_image_are_rejected(self):
        for response in (httpx.Response(403), httpx.Response(200, text="expired image")):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: response)
            ) as client:
                with self.assertRaises(MediaError):
                    await image_data_uri("https://example.test/image", client, limit=100)

    async def test_base64_is_validated_and_bounded(self):
        async with httpx.AsyncClient() as client:
            valid = await image_data_uri(
                "base64://" + base64.b64encode(PNG).decode(), client, limit=100
            )
            self.assertTrue(valid.startswith("data:image/png;base64,"))
            for bad in ("base64://!!!!", "base64://" + "a" * 1000):
                with self.assertRaises(MediaError):
                    await image_data_uri(bad, client, limit=100)

    async def test_local_file_uri_preserves_percent_and_unicode_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "通知 a%20.png"
            path.write_bytes(PNG)
            async with httpx.AsyncClient() as client:
                actual = await image_data_uri(path.as_uri(), client, limit=100)
                self.assertTrue(actual.startswith("data:image/png;base64,"))
                with self.assertRaises(MediaError):
                    await image_data_uri(str(path), client, limit=1)


if __name__ == "__main__":
    unittest.main()
