import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.db as db
import app.main as main
from app.ai import catalog, client, draft as ai_draft, jobs, prompts, writer
from app.models import AiBrand, AiDraft, DouyinAccount, PublishTask


class DisplayWidthTests(unittest.TestCase):
    def test_cjk_counts_double_and_ascii_counts_single(self):
        self.assertEqual(ai_draft.display_width("abc123"), 6)
        self.assertEqual(ai_draft.display_width("信用卡"), 6)
        self.assertEqual(ai_draft.display_width("卡 3"), 4)

    def test_emoji_counts_two_regardless_of_code_points(self):
        self.assertEqual(ai_draft.display_width("👍"), 2)
        # 带肤色修饰的是一个字素簇,不是两个字
        self.assertEqual(ai_draft.display_width("👍🏽"), 2)

    def test_nineteen_chinese_chars_is_the_practical_ceiling(self):
        self.assertLessEqual(ai_draft.display_width("一" * 19), ai_draft.SAFE_TITLE_MAX_WIDTH)
        self.assertGreater(ai_draft.display_width("一" * 20), ai_draft.SAFE_TITLE_MAX_WIDTH)


def _good_draft() -> ai_draft.Draft:
    return ai_draft.Draft(
        title="每月多拿两百的记账法",
        content="今天讲讲我自己怎么记的。\n\n先把门槛写下来，再把截止日写下来。\n\n你手里哪张卡最容易忘？",
        tags=["记账", "省钱", "信用卡"],
        cards=[
            ai_draft.Card(template="cover", data={"title": "封面"}),
            ai_draft.Card(template="tips", data={"title": "要点"}),
        ],
    )


class ValidateDraftTests(unittest.TestCase):
    def test_clean_draft_has_no_problems(self):
        self.assertEqual(ai_draft.validate_draft(_good_draft()), [])

    def test_rejects_url_in_body(self):
        d = _good_draft()
        d.content += "\n详情看 https://example.com"
        problems = ai_draft.validate_draft(d)
        self.assertTrue(any("网址" in p for p in problems), problems)

    def test_rejects_url_printed_on_a_card(self):
        d = _good_draft()
        d.cards[1].data["desc"] = "去 www.example.com 领"
        problems = ai_draft.validate_draft(d)
        self.assertTrue(any("第 2 张卡" in p for p in problems), problems)

    def test_no_card_key_is_exempt_from_the_risk_word_check(self):
        # 上游那套流水线放过 image / photo(渲染器的输入);这里的模板一个都不吃
        # 远程图,留着豁免只会变成「把网址藏在这个键名下就能穿过预检」。
        d = _good_draft()
        d.cards[1].data["image"] = "https://cdn.example.com/a.jpg"
        problems = ai_draft.validate_draft(d)
        self.assertTrue(any("第 2 张卡" in p for p in problems), problems)

    def test_rejects_overlong_title(self):
        d = _good_draft()
        d.title = "一" * 25
        problems = ai_draft.validate_draft(d)
        self.assertTrue(any("标题过长" in p for p in problems), problems)

    def test_rejects_unknown_template(self):
        d = _good_draft()
        d.cards.append(ai_draft.Card(template="magic", data={}))
        problems = ai_draft.validate_draft(d, known_templates={"cover", "tips"})
        self.assertTrue(any("magic" in p for p in problems), problems)

    def test_rejects_card_count_outside_the_requested_range(self):
        problems = ai_draft.validate_draft(_good_draft(), cards_range=(4, 7))
        self.assertTrue(any("超出这次要求" in p for p in problems), problems)

    def test_rejects_hash_prefixed_tags(self):
        d = _good_draft()
        d.tags = ["#记账"]
        problems = ai_draft.validate_draft(d)
        self.assertTrue(any("# 前缀" in p for p in problems), problems)

    def test_banned_extra_matches_card_data_too(self):
        d = _good_draft()
        d.cards[1].data["note"] = "老王小卖部 3 号窗"
        problems = ai_draft.validate_draft(d, banned_extra=("老王小卖部",))
        self.assertTrue(any("不能公开" in p for p in problems), problems)

    def test_reports_missing_and_empty_rendered_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "01.png"
            empty.touch()
            problems = ai_draft.validate_draft(
                _good_draft(), [empty, Path(tmp) / "nope.png"])
            self.assertTrue(any("空文件" in p for p in problems), problems)
            self.assertTrue(any("图片不存在" in p for p in problems), problems)

    def test_from_dict_tolerates_a_comma_joined_tag_string(self):
        d = ai_draft.Draft.from_dict({"title": "x", "content": "y", "tags": "#a, b 、c"})
        self.assertEqual(d.tags, ["a", "b", "c"])


class ExtractJsonTests(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(client.extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_block(self):
        self.assertEqual(client.extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_prose_before_and_after(self):
        text = '好的，这是草稿：\n{"a": {"b": 2}}\n希望有帮助。'
        self.assertEqual(client.extract_json(text), {"a": {"b": 2}})

    def test_non_json_raises_with_a_readable_message(self):
        with self.assertRaises(client.AiError):
            client.extract_json("我写不了")

    def test_empty_raises(self):
        with self.assertRaises(client.AiError):
            client.extract_json("   ")


class TruncatedOutputTests(unittest.TestCase):
    # 截断的 JSON 到 extract_json 那里只会变成「不是 JSON」,把人引向
    # 「模型不听话」;真正要做的只是把最大输出长度调大。所以要单独报。
    def _channel(self, provider="openai"):
        return client.AiChannel(provider=provider, base_url="https://x/v1",
                                api_key="k", model="m")

    def test_openai_length_finish_reason_says_to_raise_the_cap(self):
        class Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": '{"title": "半'},
                                     "finish_reason": "length"}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 16000}}

        async def fake_post(cli, url, headers, body):
            return Resp()

        with patch.object(client, "_post", fake_post):
            with self.assertRaises(client.AiError) as caught:
                asyncio.run(client.complete_json(self._channel(), "s", "u"))
        self.assertIn("最大输出长度", str(caught.exception))

    def test_anthropic_max_tokens_stop_reason_says_to_raise_the_cap(self):
        class Block:
            type = "text"
            text = '{"title": "半'

        class Msg:
            stop_reason = "max_tokens"
            content = [Block()]
            usage = type("U", (), {"input_tokens": 100, "output_tokens": 16000})()

        class FakeClient:
            def __init__(self, **kw):
                self.messages = self

            async def create(self, **kw):
                return Msg()

            async def close(self):
                pass

        with patch.dict("sys.modules", {"anthropic": type(
                "M", (), {"AsyncAnthropic": FakeClient})}):
            with self.assertRaises(client.AiError) as caught:
                asyncio.run(client.complete_json(
                    self._channel("anthropic"), "s", "u"))
        self.assertIn("最大输出长度", str(caught.exception))

    def test_anthropic_refusal_is_reported_as_such(self):
        class Msg:
            stop_reason = "refusal"
            content = []
            usage = type("U", (), {"input_tokens": 1, "output_tokens": 0})()

        class FakeClient:
            def __init__(self, **kw):
                self.messages = self

            async def create(self, **kw):
                return Msg()

            async def close(self):
                pass

        with patch.dict("sys.modules", {"anthropic": type(
                "M", (), {"AsyncAnthropic": FakeClient})}):
            with self.assertRaises(client.AiError) as caught:
                asyncio.run(client.complete_json(
                    self._channel("anthropic"), "s", "u"))
        self.assertIn("拒绝", str(caught.exception))


class ChannelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "ai.db"))

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def test_falls_back_to_the_auto_comment_channel(self):
        main.set_setting("ai_base_url", "https://gw.example.com/v1")
        main.set_setting("ai_api_key", "sk-shared")
        main.set_setting("ai_model", "some-model")
        ch = client.channel_from_settings()
        self.assertEqual(ch.provider, "openai")
        self.assertEqual(ch.base_url, "https://gw.example.com/v1")
        self.assertEqual(ch.model, "some-model")
        self.assertIsNone(ch.check())

    def test_content_settings_win_over_the_shared_ones(self):
        main.set_setting("ai_base_url", "https://gw.example.com/v1")
        main.set_setting("ai_api_key", "sk-shared")
        main.set_setting("ai_model", "cheap-model")
        main.set_setting("ai_content_model", "expensive-model")
        self.assertEqual(client.channel_from_settings().model, "expensive-model")

    def test_anthropic_provider_gets_its_own_defaults(self):
        main.set_setting("ai_content_provider", "anthropic")
        main.set_setting("ai_content_api_key", "sk-ant-x")
        ch = client.channel_from_settings()
        self.assertEqual(ch.base_url, client.ANTHROPIC_DEFAULT_BASE)
        self.assertEqual(ch.model, client.ANTHROPIC_DEFAULT_MODEL)
        self.assertIsNone(ch.check())

    def test_blank_key_override_keeps_the_saved_one(self):
        main.set_setting("ai_content_api_key", "sk-saved")
        ch = client.channel_from_settings({"ai_content_api_key": ""})
        self.assertEqual(ch.api_key, "sk-saved")

    def test_unconfigured_channel_explains_itself(self):
        self.assertIn("模型", client.channel_from_settings().check() or "")


class CatalogTests(unittest.TestCase):
    def test_every_template_has_a_schema_and_a_sample(self):
        names = catalog.template_names()
        self.assertIn(catalog.COVER_TEMPLATE, names)
        for name in names:
            t = catalog.templates()[name]
            self.assertTrue(t.summary, name)
            self.assertTrue(t.sample, name)
            self.assertTrue(any(not k.startswith("_") for k in t.schema), name)

    def test_prompt_block_lists_the_fields(self):
        block = catalog.templates()["cover"].as_prompt_block()
        self.assertIn("`cover`", block)
        self.assertIn("`title`", block)
        self.assertNotIn("_card", block)

    def test_themes_all_have_a_css_file(self):
        self.assertTrue(catalog.themes())
        for name in catalog.themes():
            self.assertTrue(catalog.theme_css(name).strip(), name)

    def test_unknown_theme_falls_back_to_no_extra_css(self):
        self.assertEqual(catalog.theme_css("nope"), "")
        self.assertEqual(catalog.theme_css("../templates/_base"), "")


class PromptTests(unittest.TestCase):
    def test_render_brand_always_has_every_key_the_templates_touch(self):
        b = prompts.brand_for_render({})
        self.assertEqual(set(b), {"mark", "default_date_label", "footer_note", "product"})
        self.assertIn("name_zh", b["product"])

    def test_mark_falls_back_to_the_account_name(self):
        self.assertEqual(prompts.brand_for_render({"name": "小账本"})["mark"], "小账本")

    def test_user_prompt_says_do_not_invent_numbers_when_no_facts_given(self):
        p = prompts.build_user_prompt({"name": "x"}, direction="随便写")
        self.assertIn("一个都不要编", p)

    def test_user_prompt_carries_the_facts_and_the_template_schemas(self):
        p = prompts.build_user_prompt({"name": "x"}, facts="今天返现 12 元",
                                      template_names=["cover"])
        self.assertIn("今天返现 12 元", p)
        self.assertIn("`cover`", p)
        self.assertNotIn("`tips`", p)

    def test_retry_prompt_repeats_the_problems_verbatim(self):
        p = prompts.build_retry_prompt(["标题过长:42 显示单位"])
        self.assertIn("标题过长:42 显示单位", p)


class WriteDraftTests(unittest.TestCase):
    def _channel(self):
        return client.AiChannel(provider="openai", base_url="https://x/v1",
                                api_key="k", model="m")

    def test_returns_on_the_first_clean_draft(self):
        payload = _good_draft().to_dict()
        calls = []

        async def fake(channel, system, user, history=None):
            calls.append(user)
            return payload, {"input": 10, "output": 20}

        with patch.object(writer, "complete_json", fake):
            result = asyncio.run(writer.write_draft(
                self._channel(), {"name": "x"}, cards_min=2, cards_max=3))
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.usage, {"input": 10, "output": 20})

    def test_feeds_the_problems_back_and_accepts_the_fixed_version(self):
        bad = _good_draft()
        bad.title = "一" * 25
        payloads = [bad.to_dict(), _good_draft().to_dict()]
        seen_prompts = []

        async def fake(channel, system, user, history=None):
            seen_prompts.append(user)
            return payloads[len(seen_prompts) - 1], {"input": 1, "output": 1}

        with patch.object(writer, "complete_json", fake):
            result = asyncio.run(writer.write_draft(
                self._channel(), {"name": "x"}, cards_min=2, cards_max=3))
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertIn("标题过长", seen_prompts[1])

    def test_gives_up_after_max_attempts_but_still_returns_the_draft(self):
        bad = _good_draft()
        bad.title = "一" * 25

        async def fake(channel, system, user, history=None):
            return bad.to_dict(), {"input": 1, "output": 1}

        with patch.object(writer, "complete_json", fake):
            result = asyncio.run(writer.write_draft(
                self._channel(), {"name": "x"}, cards_min=2, cards_max=3,
                max_attempts=2))
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.draft.title, "一" * 25)

    def test_banned_extra_comes_from_the_brand(self):
        self.assertEqual(writer._banned_extra({"banned_words": "甲, 乙\n丙、丁"}),
                         ("甲", "乙", "丙", "丁"))

    def test_card_range_is_sanitised(self):
        self.assertEqual(writer._sane_range(0, 0), (1, 1))
        self.assertEqual(writer._sane_range(7, 3), (7, 7))
        self.assertEqual(writer._sane_range(4, 99), (4, 18))


class AiApiTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "ai-api.db"))
        main.set_setting("ai_base_url", "https://gw.example.com/v1")
        main.set_setting("ai_api_key", "sk-x")
        main.set_setting("ai_model", "m")
        self._drafts_dir = jobs.DRAFTS_DIR
        jobs.DRAFTS_DIR = Path(self.tmp.name) / "ai_drafts"

    def tearDown(self):
        jobs.DRAFTS_DIR = self._drafts_dir
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _brand(self, **over):
        fields = {"name": "小账本", "mark": "小账本", "theme": "ledger",
                  "cards_min": 2, "cards_max": 3, **over}
        return asyncio.run(main.create_ai_brand(main.AiBrandIn(**fields)))

    def _account(self) -> int:
        with db.get_session() as s:
            acc = DouyinAccount(platform="xhs", nickname="发布号", status="active",
                                storage_state="{}", creator_storage_state="{}")
            s.add(acc); s.commit(); s.refresh(acc)
            return acc.id

    def _ready_draft(self, brand_id: int, images: list[Path] | None = None) -> int:
        d = _good_draft()
        with db.get_session() as s:
            row = AiDraft(
                brand_id=brand_id, platform="xhs", status=jobs.STATUS_READY,
                title=d.title, content=d.content,
                tags_json=json.dumps(d.tags, ensure_ascii=False),
                cards_json=json.dumps([c.to_dict() for c in d.cards], ensure_ascii=False),
                images_json=json.dumps([str(p) for p in (images or [])]),
            )
            s.add(row); s.commit(); s.refresh(row)
            return row.id

    def _fake_images(self, draft_id: int, n: int) -> list[Path]:
        out = jobs.draft_dir(draft_id)
        out.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(n):
            p = out / f"{i + 1:02d}-cover.png"
            p.write_bytes(b"\x89PNG fake")
            paths.append(p)
        return paths

    def test_catalog_lists_templates_and_themes(self):
        payload = asyncio.run(main.ai_catalog_list())
        self.assertEqual(payload["cover_template"], "cover")
        self.assertTrue(payload["templates"])
        self.assertTrue(payload["themes"])
        self.assertIn("anthropic", payload["providers"])

    def test_brand_crud_round_trip(self):
        created = self._brand()
        self.assertEqual(created["name"], "小账本")
        listed = asyncio.run(main.list_ai_brands())
        self.assertEqual(len(listed), 1)
        asyncio.run(main.delete_ai_brand(created["id"]))
        self.assertEqual(asyncio.run(main.list_ai_brands()), [])

    def test_brand_rejects_unknown_theme_and_template(self):
        with self.assertRaises(main.HTTPException):
            self._brand(theme="no-such-theme")
        with self.assertRaises(main.HTTPException):
            self._brand(allowed_templates=["no-such-template"])

    def test_brand_rejects_inverted_card_range(self):
        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(main.create_ai_brand(
                main.AiBrandIn(name="x", cards_min=5, cards_max=2)))
        self.assertEqual(caught.exception.status_code, 400)

    def test_deleting_a_brand_keeps_its_drafts(self):
        brand = self._brand()
        did = self._ready_draft(brand["id"])
        asyncio.run(main.delete_ai_brand(brand["id"]))
        row = asyncio.run(main.get_ai_draft(did))
        self.assertIsNone(row["brand_id"])

    def test_create_draft_enqueues_a_background_job(self):
        brand = self._brand()
        with patch.object(main.ai_jobs, "enqueue", return_value=True) as enq:
            payload = asyncio.run(main.create_ai_draft(main.AiDraftIn(
                brand_id=brand["id"], direction="讲讲怎么记账")))
        self.assertEqual(payload["status"], jobs.STATUS_PENDING)
        enq.assert_called_once_with(payload["id"])

    def test_create_draft_refuses_when_the_channel_is_unconfigured(self):
        main.set_setting("ai_model", "")
        main.set_setting("ai_content_model", "")
        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(main.create_ai_draft(main.AiDraftIn(direction="x")))
        self.assertEqual(caught.exception.status_code, 400)

    def test_update_rechecks_and_reports_problems(self):
        brand = self._brand()
        did = self._ready_draft(brand["id"])
        payload = asyncio.run(main.update_ai_draft(
            did, main.AiDraftUpdate(content="来 https://example.com 领")))
        self.assertTrue(any("网址" in p for p in payload["problems"]), payload)

    def test_update_rejects_an_unknown_card_template(self):
        brand = self._brand()
        did = self._ready_draft(brand["id"])
        with self.assertRaises(main.HTTPException):
            asyncio.run(main.update_ai_draft(
                did, main.AiDraftUpdate(cards=[{"template": "magic", "data": {}}])))

    def test_to_publish_creates_a_publish_task(self):
        brand = self._brand()
        did = self._ready_draft(brand["id"])
        images = self._fake_images(did, 2)
        with db.get_session() as s:
            row = s.get(AiDraft, did)
            row.images_json = json.dumps([str(p) for p in images])
            s.add(row); s.commit()
        acc_id = self._account()
        out = asyncio.run(main.ai_draft_to_publish(
            did, main.AiToPublishIn(account_id=acc_id)))
        self.assertEqual(out["publish"]["media_count"], 2)
        self.assertEqual(out["publish"]["topics"], "记账,省钱,信用卡")
        with db.get_session() as s:
            self.assertIsNotNone(s.get(AiDraft, did).publish_task_id)
            self.assertEqual(s.exec(main.select(PublishTask)).first().desc,
                             _good_draft().content)

    def test_to_publish_refuses_a_draft_that_fails_preflight(self):
        brand = self._brand()
        did = self._ready_draft(brand["id"], self._fake_images(1, 0))
        with db.get_session() as s:
            row = s.get(AiDraft, did)
            row.images_json = json.dumps([str(p) for p in self._fake_images(did, 2)])
            row.content = "加我微信 kaquan2026"
            s.add(row); s.commit()
        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(main.ai_draft_to_publish(
                did, main.AiToPublishIn(account_id=self._account())))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("预检没过", caught.exception.detail)

    def test_to_publish_refuses_when_the_images_are_gone(self):
        brand = self._brand()
        did = self._ready_draft(brand["id"], [Path("/nope/01.png")])
        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(main.ai_draft_to_publish(
                did, main.AiToPublishIn(account_id=self._account())))
        self.assertIn("配图", caught.exception.detail)

    def test_to_publish_refuses_a_draft_that_is_not_ready(self):
        brand = self._brand()
        did = self._ready_draft(brand["id"])
        with db.get_session() as s:
            row = s.get(AiDraft, did)
            row.status = jobs.STATUS_GENERATING
            s.add(row); s.commit()
        with self.assertRaises(main.HTTPException):
            asyncio.run(main.ai_draft_to_publish(
                did, main.AiToPublishIn(account_id=self._account())))

    def test_image_route_refuses_a_path_outside_the_draft_directory(self):
        brand = self._brand()
        outside = Path(self.tmp.name) / "secret.png"
        outside.write_bytes(b"x")
        did = self._ready_draft(brand["id"], [outside])
        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(main.ai_draft_image(did, 0))
        self.assertEqual(caught.exception.status_code, 404)

    def test_image_route_serves_a_file_inside_the_draft_directory(self):
        brand = self._brand()
        did = self._ready_draft(brand["id"])
        images = self._fake_images(did, 1)
        with db.get_session() as s:
            row = s.get(AiDraft, did)
            row.images_json = json.dumps([str(p) for p in images])
            s.add(row); s.commit()
        resp = asyncio.run(main.ai_draft_image(did, 0))
        self.assertEqual(Path(resp.path).resolve(), images[0].resolve())

    def test_delete_draft_removes_its_rendered_files(self):
        brand = self._brand()
        did = self._ready_draft(brand["id"])
        self._fake_images(did, 2)
        asyncio.run(main.delete_ai_draft(did))
        self.assertFalse(jobs.draft_dir(did).exists())

    def test_sources_endpoint_lists_the_woolworths_source(self):
        rows = asyncio.run(main.list_ai_sources())
        names = [r["name"] for r in rows]
        self.assertIn("woolworths_nz_specials", names)
        spec = next(r for r in rows if r["name"] == "woolworths_nz_specials")
        self.assertIn("url", spec["default_config"])

    def test_brand_round_trips_its_facts_source(self):
        created = self._brand(facts_source="woolworths_nz_specials",
                              facts_config={"pick": 2}, facts_dedup_drafts=9)
        self.assertEqual(created["facts_source"], "woolworths_nz_specials")
        self.assertEqual(created["facts_config"], {"pick": 2})
        self.assertEqual(created["facts_dedup_drafts"], 9)

    def test_brand_rejects_an_unknown_facts_source(self):
        with self.assertRaises(main.HTTPException) as caught:
            self._brand(facts_source="no-such-source")
        self.assertEqual(caught.exception.status_code, 400)

    def test_facts_pull_returns_the_text_without_saving_it(self):
        brand = self._brand(facts_source="woolworths_nz_specials")

        async def fake(b, *, exclude=None):
            return jobs.sources.Facts(text="拉回来的素材", keys=["k"], note="ok")

        with patch.object(main.ai_jobs, "pull_facts", fake):
            out = asyncio.run(main.pull_ai_facts(
                main.AiFactsPullIn(brand_id=brand["id"])))
        self.assertEqual(out["facts"], "拉回来的素材")
        self.assertEqual(out["keys"], ["k"])
        self.assertEqual(asyncio.run(main.list_ai_drafts()), [])

    def test_facts_pull_needs_a_source(self):
        brand = self._brand()
        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(main.pull_ai_facts(main.AiFactsPullIn(brand_id=brand["id"])))
        self.assertEqual(caught.exception.status_code, 400)

    def test_create_draft_stores_the_pulled_source_keys(self):
        brand = self._brand()
        with patch.object(main.ai_jobs, "enqueue", return_value=True):
            payload = asyncio.run(main.create_ai_draft(main.AiDraftIn(
                brand_id=brand["id"], facts="贴好的素材", source_keys=["a", "b"])))
        self.assertEqual(payload["source_keys"], ["a", "b"])

    def test_settings_round_trip_the_content_channel(self):
        out = asyncio.run(main.put_settings(main.SettingsIn(
            ai_content_provider="anthropic", ai_content_model="claude-opus-5",
            ai_content_api_key="sk-ant-x", ai_content_max_tokens="32000")))
        self.assertEqual(out["ai_content_provider"], "anthropic")
        self.assertTrue(out["ai_content_api_key_set"])
        self.assertTrue(out["ai_content_effective"]["ready"])

    def test_settings_reject_a_bad_provider(self):
        with self.assertRaises(main.HTTPException):
            asyncio.run(main.put_settings(main.SettingsIn(ai_content_provider="grok")))

    def test_settings_reject_an_out_of_range_max_tokens(self):
        with self.assertRaises(main.HTTPException):
            asyncio.run(main.put_settings(main.SettingsIn(ai_content_max_tokens="10")))


class SweepInterruptedTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "sweep.db"))

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def test_running_rows_are_failed_and_ready_rows_are_left_alone(self):
        with db.get_session() as s:
            for status in (jobs.STATUS_PENDING, jobs.STATUS_GENERATING,
                           jobs.STATUS_RENDERING, jobs.STATUS_READY):
                s.add(AiDraft(status=status))
            s.commit()
        self.assertEqual(jobs.sweep_interrupted(), 3)
        with db.get_session() as s:
            rows = s.exec(main.select(AiDraft)).all()
            statuses = sorted(r.status for r in rows)
            self.assertEqual(statuses, ["failed", "failed", "failed", "ready"])
            self.assertTrue(any("重启" in (r.error or "") for r in rows))

    def test_allowed_templates_falls_back_when_the_whitelist_is_stale(self):
        brand = AiBrand(name="x", allowed_templates=json.dumps(["ghost"]))
        self.assertEqual(jobs.allowed_templates(brand), catalog.template_names())
        brand.allowed_templates = json.dumps(["cover", "ghost"])
        self.assertEqual(jobs.allowed_templates(brand), ["cover"])

    def test_draft_dir_is_absolute_even_when_the_root_is_relative(self):
        # 渲染要把 HTML 变成 file:// URI,相对路径会当场抛 ValueError,
        # 而报出来的「失败」完全看不出跟目录有关。
        previous = jobs.DRAFTS_DIR
        try:
            jobs.DRAFTS_DIR = Path("./data/ai_drafts")
            d = jobs.draft_dir(7)
            self.assertTrue(d.is_absolute(), d)
            d.as_uri()
        finally:
            jobs.DRAFTS_DIR = previous

    def test_recent_titles_only_looks_at_ready_drafts_of_that_brand(self):
        with db.get_session() as s:
            s.add(AiDraft(brand_id=1, status=jobs.STATUS_READY, title="甲"))
            s.add(AiDraft(brand_id=1, status=jobs.STATUS_FAILED, title="乙"))
            s.add(AiDraft(brand_id=2, status=jobs.STATUS_READY, title="丙"))
            s.commit()
        self.assertEqual(jobs.recent_titles(1), ["甲"])
        self.assertEqual(jobs.recent_titles(None), [])


class WoolworthsSourceTests(unittest.TestCase):
    # 素材源是这条链上唯一碰真实价格的地方 —— 抄错一个数,读者就白跑一趟。
    def _payload(self, n=10):
        items = []
        for i in range(n):
            pct = 50 - i * 5
            items.append({
                "type": "Product", "sku": f"sku{i}",
                "name": f"chicken item {i}" if i % 3 == 0 else f"candy item {i}",
                "brand": f"brand{i}",
                "unit": "Each", "size": {"volumeSize": f"{100 + i}g"},
                "images": {"big": f"https://cdn/x{i}.jpg?impolicy=p&w=200&h=200"},
                "price": {"salePrice": 10 - i, "originalPrice": 20 - i,
                          "savePercentage": pct, "savePrice": 10},
            })
        return {"products": {"items": items}}

    def _fetch(self, payload=None, *, config=None, exclude=(), seed="t"):
        from app.ai.sources import woolworths

        class Resp:
            status_code = 200
            text = ""

            def json(self):
                return payload if payload is not None else self_payload

        self_payload = self._payload()

        class Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                Client.seen = {"url": url, "headers": headers or {}}
                return Resp()

        self.client = Client
        with patch.object(woolworths.httpx, "AsyncClient", Client):
            return asyncio.run(woolworths.fetch(
                {**woolworths.DEFAULT_CONFIG, **(config or {})},
                exclude=list(exclude), seed=seed))

    def test_prices_are_copied_verbatim_and_images_upsized(self):
        facts = self._fetch()
        self.assertIn("$10.00", facts.text)      # salePrice 10 → 补两位小数
        self.assertIn("50% OFF", facts.text)
        self.assertIn("w=1200&h=1200", facts.text)
        self.assertNotIn("w=200&h=200", facts.text)

    def test_sends_the_header_that_the_api_needs(self):
        self._fetch()
        self.assertEqual(self.client.seen["headers"].get("x-requested-with"),
                         "OnlineShopping.WebApp")

    def test_picks_the_requested_count_and_reports_keys(self):
        facts = self._fetch(config={"pick": 3})
        self.assertEqual(len(facts.keys), 3)
        self.assertEqual(len(set(facts.keys)), 3)

    def test_excluded_skus_are_not_picked_again(self):
        first = self._fetch(config={"pick": 2}, seed="a")
        again = self._fetch(config={"pick": 2}, exclude=first.keys, seed="a")
        self.assertFalse(set(first.keys) & set(again.keys))

    def test_different_days_give_different_combinations(self):
        combos = {tuple(sorted(self._fetch(config={"pick": 3}, seed=f"day{i}").keys))
                  for i in range(8)}
        # 特价一周才换一次;按折扣排序的话这里会只有 1 种组合
        self.assertGreater(len(combos), 1)

    def test_running_out_of_fresh_items_reuses_the_oldest_and_says_so(self):
        every = [f"sku{i}" for i in range(10)]
        facts = self._fetch(config={"pick": 3}, exclude=every)
        self.assertEqual(len(facts.keys), 3)
        self.assertIn("最近发过", facts.text)

    def test_relaxes_the_threshold_when_the_week_is_thin_and_says_so(self):
        payload = self._payload(2)          # 只有 50% 和 45% 两样
        facts = self._fetch(payload, config={"pick": 3, "min_percent": 48})
        self.assertIn("门槛已放宽", facts.text)

    def test_same_brand_is_not_picked_twice(self):
        payload = self._payload(6)
        for it in payload["products"]["items"]:
            it["brand"] = "同一个牌子"
        facts = self._fetch(payload, config={"pick": 3})
        self.assertEqual(len(facts.keys), 1)

    def test_staple_keywords_pull_everyday_items_up(self):
        # chicken 那几样折扣更浅,靠加权才上得来;跑多次看它出现的频率
        hits = 0
        for i in range(30):
            facts = self._fetch(config={"pick": 1, "min_percent": 0}, seed=f"s{i}")
            if any(k in ("sku0", "sku3", "sku6", "sku9") for k in facts.keys):
                hits += 1
        self.assertGreater(hits, 6, "日常品加权看起来没生效")

    def test_items_without_a_price_are_dropped(self):
        payload = self._payload(4)
        payload["products"]["items"][0]["price"] = {}
        facts = self._fetch(payload, config={"pick": 3, "min_percent": 0})
        self.assertNotIn("sku0", facts.keys)

    def test_http_error_becomes_a_readable_message(self):
        from app.ai.sources import woolworths, SourceError

        class Boom:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                raise woolworths.httpx.ConnectError("nope")

        with patch.object(woolworths.httpx, "AsyncClient", Boom):
            with self.assertRaises(SourceError) as caught:
                asyncio.run(woolworths.fetch(woolworths.DEFAULT_CONFIG, exclude=[]))
        self.assertIn("连不上", str(caught.exception))


class FactsSourceWiringTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "facts.db"))

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def test_recent_source_keys_are_newest_first_and_deduped(self):
        with db.get_session() as s:
            s.add(AiDraft(brand_id=1, source_keys_json=json.dumps(["a", "b"])))
            s.add(AiDraft(brand_id=1, source_keys_json=json.dumps(["b", "c"])))
            s.add(AiDraft(brand_id=2, source_keys_json=json.dumps(["z"])))
            s.commit()
        self.assertEqual(jobs.recent_source_keys(1), ["b", "c", "a"])
        self.assertEqual(jobs.recent_source_keys(1, 1), ["b", "c"])
        self.assertEqual(jobs.recent_source_keys(None), [])

    def test_pull_facts_rejects_a_brand_without_a_source(self):
        with self.assertRaises(client.AiError):
            asyncio.run(jobs.pull_facts(AiBrand(name="x")))

    def test_pull_facts_rejects_broken_config_json(self):
        b = AiBrand(name="x", facts_source="woolworths_nz_specials",
                    facts_config="{not json")
        with self.assertRaises(client.AiError) as caught:
            asyncio.run(jobs.pull_facts(b))
        self.assertIn("JSON", str(caught.exception))

    def test_pull_facts_passes_the_config_through(self):
        seen = {}

        async def fake(name, config, *, exclude, seed=None):
            seen.update({"name": name, "config": config, "exclude": exclude})
            return jobs.sources.Facts(text="素材", keys=["k1"], note="n")

        b = AiBrand(id=9, name="x", facts_source="woolworths_nz_specials",
                    facts_config=json.dumps({"pick": 2}), facts_dedup_drafts=3)
        with patch.object(jobs.sources, "fetch", fake):
            got = asyncio.run(jobs.pull_facts(b))
        self.assertEqual(got.keys, ["k1"])
        self.assertEqual(seen["config"], {"pick": 2})
        self.assertEqual(seen["name"], "woolworths_nz_specials")

    def test_unknown_source_is_a_readable_error(self):
        b = AiBrand(name="x", facts_source="no-such-source")
        with self.assertRaises(client.AiError) as caught:
            asyncio.run(jobs.pull_facts(b))
        self.assertIn("没有这个素材源", str(caught.exception))


class RunDraftJobTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "job.db"))
        main.set_setting("ai_base_url", "https://gw.example.com/v1")
        main.set_setting("ai_api_key", "sk-x")
        main.set_setting("ai_model", "m")
        self._drafts_dir = jobs.DRAFTS_DIR
        jobs.DRAFTS_DIR = Path(self.tmp.name) / "ai_drafts"

    def tearDown(self):
        jobs.DRAFTS_DIR = self._drafts_dir
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _row(self, brand: AiBrand | None = None) -> int:
        with db.get_session() as s:
            bid = None
            if brand is not None:
                s.add(brand); s.commit(); s.refresh(brand)
                bid = brand.id
            row = AiDraft(brand_id=bid, direction="讲讲怎么记账",
                          status=jobs.STATUS_PENDING)
            s.add(row); s.commit(); s.refresh(row)
            return row.id

    def _fake_render(self, cards, out_dir, **kw):
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, c in enumerate(cards, start=1):
            p = out_dir / f"{i:02d}-{c.template}.png"
            p.write_bytes(b"\x89PNG fake")
            paths.append(p)
        return paths

    def test_happy_path_writes_content_images_and_marks_ready(self):
        did = self._row(AiBrand(name="小账本", cards_min=2, cards_max=3, theme="ledger"))

        async def fake_json(channel, system, user, history=None):
            return _good_draft().to_dict(), {"input": 5, "output": 7}

        async def fake_render(cards, out_dir, **kw):
            return self._fake_render(cards, out_dir, **kw)

        with patch.object(writer, "complete_json", fake_json), \
             patch.object(jobs.render, "render_cards", fake_render):
            asyncio.run(jobs.run_draft_job(did))

        with db.get_session() as s:
            row = s.get(AiDraft, did)
            self.assertEqual(row.status, jobs.STATUS_READY)
            self.assertEqual(row.title, _good_draft().title)
            self.assertEqual(json.loads(row.problems_json), [])
            self.assertEqual(len(json.loads(row.images_json)), 2)
            self.assertEqual(json.loads(row.usage_json), {"input": 5, "output": 7})
            self.assertEqual(row.model, "openai:m")
            self.assertIsNotNone(row.finished_at)

    def test_empty_facts_are_auto_pulled_when_the_brand_has_a_source(self):
        brand = AiBrand(name="小账本", cards_min=2, cards_max=3,
                        facts_source="woolworths_nz_specials")
        did = self._row(brand)
        seen = {}

        async def fake_pull(b, *, exclude=None):
            seen["brand"] = b.name
            return jobs.sources.Facts(text="今天鸡腿 $4.79", keys=["sku-x"], note="n")

        async def fake_json(channel, system, user, history=None):
            seen["prompt"] = user
            return _good_draft().to_dict(), {"input": 1, "output": 1}

        async def fake_render(cards, out_dir, **kw):
            return self._fake_render(cards, out_dir, **kw)

        with patch.object(jobs, "pull_facts", fake_pull), \
             patch.object(writer, "complete_json", fake_json), \
             patch.object(jobs.render, "render_cards", fake_render):
            asyncio.run(jobs.run_draft_job(did))

        self.assertEqual(seen["brand"], "小账本")
        self.assertIn("今天鸡腿 $4.79", seen["prompt"])
        with db.get_session() as s:
            row = s.get(AiDraft, did)
            self.assertEqual(row.facts, "今天鸡腿 $4.79")
            # 用掉的条目要记下来,否则明天还会抽到同样的商品
            self.assertEqual(json.loads(row.source_keys_json), ["sku-x"])

    def test_hand_written_facts_are_never_overwritten_by_the_source(self):
        brand = AiBrand(name="小账本", cards_min=2, cards_max=3,
                        facts_source="woolworths_nz_specials")
        did = self._row(brand)
        with db.get_session() as s:
            row = s.get(AiDraft, did)
            row.facts = "我自己贴的素材"
            s.add(row); s.commit()

        async def boom(b, *, exclude=None):
            raise AssertionError("手填了素材还去拉,会把人改的东西覆盖掉")

        async def fake_json(channel, system, user, history=None):
            return _good_draft().to_dict(), {"input": 1, "output": 1}

        async def fake_render(cards, out_dir, **kw):
            return self._fake_render(cards, out_dir, **kw)

        with patch.object(jobs, "pull_facts", boom), \
             patch.object(writer, "complete_json", fake_json), \
             patch.object(jobs.render, "render_cards", fake_render):
            asyncio.run(jobs.run_draft_job(did))
        with db.get_session() as s:
            self.assertEqual(s.get(AiDraft, did).facts, "我自己贴的素材")

    def test_a_failing_source_fails_the_draft_with_its_reason(self):
        brand = AiBrand(name="x", facts_source="woolworths_nz_specials")
        did = self._row(brand)

        async def boom(b, *, exclude=None):
            raise client.AiError("连不上 Woolworths 特价接口")

        with patch.object(jobs, "pull_facts", boom):
            asyncio.run(jobs.run_draft_job(did))
        with db.get_session() as s:
            row = s.get(AiDraft, did)
            self.assertEqual(row.status, jobs.STATUS_FAILED)
            self.assertIn("Woolworths", row.error)

    def test_channel_failure_lands_on_the_row_as_a_readable_error(self):
        did = self._row()

        async def boom(channel, system, user, history=None):
            raise client.AiError("HTTP 401:key 不对")

        with patch.object(writer, "complete_json", boom):
            asyncio.run(jobs.run_draft_job(did))

        with db.get_session() as s:
            row = s.get(AiDraft, did)
            self.assertEqual(row.status, jobs.STATUS_FAILED)
            self.assertIn("401", row.error)

    def test_render_failure_also_lands_on_the_row(self):
        did = self._row()

        async def fake_json(channel, system, user, history=None):
            return _good_draft().to_dict(), {"input": 1, "output": 1}

        async def boom(cards, out_dir, **kw):
            raise RuntimeError("起不来浏览器")

        with patch.object(writer, "complete_json", fake_json), \
             patch.object(jobs.render, "render_cards", boom):
            asyncio.run(jobs.run_draft_job(did))

        with db.get_session() as s:
            row = s.get(AiDraft, did)
            self.assertEqual(row.status, jobs.STATUS_FAILED)
            self.assertIn("起不来浏览器", row.error)

    def test_a_draft_that_fails_preflight_is_still_ready_with_its_problems(self):
        did = self._row()
        bad = _good_draft()
        bad.content += "\n看 https://example.com"

        async def fake_json(channel, system, user, history=None):
            return bad.to_dict(), {"input": 1, "output": 1}

        async def fake_render(cards, out_dir, **kw):
            return self._fake_render(cards, out_dir, **kw)

        with patch.object(writer, "complete_json", fake_json), \
             patch.object(jobs.render, "render_cards", fake_render):
            asyncio.run(jobs.run_draft_job(did))

        with db.get_session() as s:
            row = s.get(AiDraft, did)
            # 有产出就是 ready —— 人改一句就能用,整轮丢掉才是浪费
            self.assertEqual(row.status, jobs.STATUS_READY)
            self.assertTrue(any("网址" in p for p in json.loads(row.problems_json)))

    def test_rerun_clears_the_previous_pngs(self):
        did = self._row()
        stale = jobs.draft_dir(did)
        stale.mkdir(parents=True, exist_ok=True)
        (stale / "99-old.png").write_bytes(b"old")

        async def fake_json(channel, system, user, history=None):
            return _good_draft().to_dict(), {"input": 1, "output": 1}

        async def fake_render(cards, out_dir, **kw):
            return self._fake_render(cards, out_dir, **kw)

        with patch.object(writer, "complete_json", fake_json), \
             patch.object(jobs.render, "render_cards", fake_render):
            asyncio.run(jobs.run_draft_job(did))
        self.assertFalse((jobs.draft_dir(did) / "99-old.png").exists())


class RenderTests(unittest.TestCase):
    def test_every_sample_card_renders_to_html(self):
        from app.ai import render as ai_render
        brand = prompts.brand_for_render({"name": "小账本", "mark": "小账本"})
        for i, card in enumerate(ai_render.sample_cards(), start=1):
            html = ai_render.render_html(card, index=i, total=9, brand=brand,
                                         theme="clinical")
            self.assertIn("canvas", html, card.template)
            self.assertIn("__cardReady", html, card.template)

    def test_optional_brand_fields_do_not_blow_up_strict_undefined(self):
        from app.ai import render as ai_render
        # 人设一个字段都没填时也必须能渲 —— StrictUndefined 下少一个键就是当场炸。
        brand = prompts.brand_for_render({})
        for i, card in enumerate(ai_render.sample_cards(), start=1):
            ai_render.render_html(card, index=i, total=9, brand=brand)

    def test_rich_filter_escapes_everything_but_the_inline_whitelist(self):
        from app.ai.render import _rich
        self.assertEqual(str(_rich("<em>红</em>")), "<em>红</em>")
        self.assertIn("&lt;script&gt;", str(_rich("<script>alert(1)</script>")))
        self.assertIn("<br>", str(_rich("上\n下")))

    def test_relative_output_dir_is_absolutised_before_screenshotting(self):
        from app.ai import render as ai_render
        import os
        shot = {}

        async def fake_shoot(pairs):
            shot["uris"] = [h.as_uri() for h, _ in pairs]
            for _, png in pairs:
                png.write_bytes(b"\x89PNG")
            return [png for _, png in pairs]

        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.object(ai_render, "_shoot", fake_shoot):
                    out = asyncio.run(ai_render.render_cards(
                        [ai_draft.Card(template="cover", data={"title": "标题"})],
                        Path("./data/ai_drafts/9"),
                        brand=prompts.brand_for_render({})))
            finally:
                os.chdir(cwd)
        self.assertTrue(all(p.is_absolute() for p in out), out)
        self.assertTrue(all(u.startswith("file:///") for u in shot["uris"]), shot)

    def test_unknown_template_is_a_render_error(self):
        from app.ai import render as ai_render
        with self.assertRaises(ai_render.RenderError):
            ai_render.render_html(ai_draft.Card(template="ghost", data={}),
                                  index=1, total=1, brand=prompts.brand_for_render({}))


if __name__ == "__main__":
    unittest.main()
