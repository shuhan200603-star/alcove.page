"""End-to-end checks for private file transport without calling Claude or Telegram."""
import importlib.util
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx


class FileBridgeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        (root / "static").mkdir()
        cls.env = patch.dict(os.environ, {
            "LLM_BACKEND": "claude_code",
            "ALCOVE_ACCESS_TOKEN": "a" * 32,
            "TELEGRAM_BOT_TOKEN": "fake-token",
            "TELEGRAM_OWNER_ID": "123",
            "ALCOVE_DATA_DB": str(root / "state.sqlite3"),
            "ALCOVE_FILES_DIR": str(root / "files"),
            "ALCOVE_STATIC_DIR": str(root / "static"),
        })
        cls.env.start()
        spec = importlib.util.spec_from_file_location("alcove_test_server", Path(__file__).with_name("server.py"))
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    async def test_web_upload_and_private_download(self):
        app = self.server.app
        transport = httpx.ASGITransport(app=app)
        headers = {"X-Alcove-Access": "a" * 32}
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            upload = await client.post("/api/files", headers={**headers, "X-File-Name": "report.md"},
                                       content=b"# Hello")
            self.assertEqual(upload.status_code, 200)
            relative = upload.json()["path"]
            self.assertEqual(self.server._read_document(relative), "# Hello")
            with self.assertRaises(ValueError):
                self.server._file_path("outbox/../../etc/passwd")
            (self.server._file_path("outbox/result.md", area="outbox")).write_text("Done")
            self.assertEqual((await client.get("/api/files/outbox/result.md")).status_code, 401)
            result = await client.get("/api/files/outbox/result.md", headers=headers)
            self.assertEqual(result.content, b"Done")
            self.assertEqual((await client.get("/api/files/inbox/report.md", headers=headers)).status_code, 400)

    async def test_telegram_document_receives_and_returns_file_once(self):
        server = self.server
        sent = []

        def telegram(request):
            if request.url.path.endswith("/getFile"):
                return httpx.Response(200, json={"ok": True, "result": {"file_path": "docs/input.txt"}})
            if request.url.path.endswith("/docs/input.txt"):
                return httpx.Response(200, content=b"original")
            if request.url.path.endswith("/sendMessage"):
                sent.append("message")
            if request.url.path.endswith("/sendDocument"):
                sent.append("document")
                self.assertIn(b"edited", request.content)
            return httpx.Response(200, json={"ok": True})

        async def answer(history, _memory, _web, produced, _model):
            self.assertIn("inbox/tg-77-input.txt", history[-1]["content"])
            self.assertEqual(server._read_document("inbox/tg-77-input.txt"), "original")
            relative = "outbox/edited.txt"
            server._file_path(relative).write_text("edited")
            produced.add(relative)
            return "修改好了"

        update = {"update_id": 77, "message": {"from": {"id": 123},
                  "chat": {"id": 123, "type": "private"},
                  "document": {"file_id": "abc", "file_name": "input.txt", "file_size": 8}}}
        with patch.object(server, "_answer", answer):
            async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as client:
                self.assertTrue(await server._telegram_process(update, client))
                self.assertTrue(await server._telegram_process(update, client))
        self.assertEqual(sent.count("message"), 1)
        self.assertEqual(sent.count("document"), 1)

    def test_docx_text(self):
        relative = "inbox/sample.docx"
        path = self.server._file_path(relative)
        with zipfile.ZipFile(path, "w") as doc:
            doc.writestr("word/document.xml",
                         '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                         '<w:body><w:p><w:r><w:t>你好</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual(self.server._read_document(relative), "你好")
        generated = self.server._file_path("outbox/generated.docx")
        self.server._write_docx(generated, "第一段\n第二段")
        self.assertEqual(self.server._read_document("outbox/generated.docx"), "第一段\n第二段")

    async def test_current_time_is_given_to_claude(self):
        captured = {}

        async def fake_reply(_msgs, system, _web, _memory, _produced, _model):
            captured["system"] = system
            return "ok"

        with patch.object(self.server, "_claude_reply", fake_reply):
            reply = await self.server._answer([{"role": "user", "content": "现在几点？"}], False, False)
        self.assertEqual(reply, "ok")
        self.assertIn("当前时间：", captured["system"])
        self.assertIn("Asia/Shanghai", captured["system"])

    async def test_telegram_model_command_does_not_call_claude(self):
        sent = []

        def telegram(request):
            sent.append(request)
            return httpx.Response(200, json={"ok": True})

        update = {"update_id": 78, "message": {"from": {"id": 123},
                  "chat": {"id": 123, "type": "private"}, "text": "/model opus"}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as client:
            self.assertTrue(await self.server._telegram_process(update, client))
        self.assertEqual(self.server._telegram_model(), "opus")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].url.path.rsplit("/", 1)[-1], "sendMessage")
        self.assertEqual(len(json.loads(sent[0].content)["reply_markup"]["inline_keyboard"][0]), 3)

    async def test_model_picker_changes_only_owners_private_chat(self):
        server = self.server
        server._telegram_set_model("sonnet")
        sent = []

        def telegram(request):
            sent.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
            return httpx.Response(200, json={"ok": True})

        command = {"update_id": 90, "message": {"from": {"id": 123},
                   "chat": {"id": 123, "type": "private"}, "text": "/model"}}
        callback = {"update_id": 91, "callback_query": {"id": "tap-1", "from": {"id": 123},
                    "data": "model:opus", "message": {"message_id": 7,
                    "chat": {"id": 123, "type": "private"}}}}
        stranger = {"update_id": 92, "callback_query": {"id": "tap-2", "from": {"id": 456},
                    "data": "model:haiku", "message": {"message_id": 7,
                    "chat": {"id": 123, "type": "private"}}}}

        with patch.object(server, "_answer", side_effect=AssertionError("Claude called")):
            async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as client:
                self.assertTrue(await server._telegram_process(command, client))
                self.assertTrue(await server._telegram_process(callback, client))
                self.assertTrue(await server._telegram_process(stranger, client))

        self.assertEqual(server._telegram_model(), "opus")
        self.assertEqual([kind for kind, _ in sent],
                         ["sendMessage", "answerCallbackQuery", "editMessageText",
                          "answerCallbackQuery"])
        keyboard = sent[0][1]["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([button["callback_data"] for button in keyboard],
                         ["model:sonnet", "model:opus", "model:haiku"])
        self.assertIn("✓ Opus", sent[2][1]["reply_markup"]["inline_keyboard"][0][1]["text"])

    async def test_usage_reset_is_readable_local_time(self):
        sent = []

        def telegram(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})

        update = {"update_id": 93, "message": {"from": {"id": 123},
                  "chat": {"id": 123, "type": "private"}, "text": "/usage"}}
        with patch.dict(self.server.PLAN_USAGE, {
            "five_hour": {"used_percentage": 5.0, "resets_at": 1792920000}
        }, clear=True):
            async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as client:
                self.assertTrue(await self.server._telegram_process(update, client))
        self.assertIn("2026-10-25 17:20（Asia/Shanghai）", sent[0]["text"])
        self.assertNotIn("1792920000", sent[0]["text"])

    async def test_command_menu_is_scoped_to_owner_chat(self):
        sent = []

        def telegram(request):
            sent.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as client:
            await self.server._telegram_register_commands(client)
        self.assertEqual(sent[0][0], "setMyCommands")
        self.assertEqual(sent[0][1]["scope"], {"type": "chat", "chat_id": 123})
        self.assertEqual([item["command"] for item in sent[0][1]["commands"]],
                         ["model", "usage", "help"])


if __name__ == "__main__":
    unittest.main()
