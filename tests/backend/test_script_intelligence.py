from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.services.script_intelligence_service import (
    ScriptIntelligenceService,
    distill_script,
)


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_distill_short_script_extracts_tags_and_template():
    result = distill_script(
        "你知道装修最大的误区是什么吗？很多人只盯着墙面。"
        "其实灯光才决定客厅氛围。评论告诉我你家最想改哪里。",
        "short_video",
    )

    assert "home" in result["tags"]["industry"]
    assert "comments" in result["tags"]["effect"]
    assert result["template"]["hook_formula"].startswith("你知道装修")


def test_distill_short_script_does_not_learn_giveaway_cta():
    result = distill_script(
        "郑和下西洋让明朝和海外港口发生了真实交流。"
        "评论你的看法，三位随机评论者将获赠一杯茶。",
        "short_video",
    )

    cta = result["template"]["cta_pattern"]
    assert "随机评论者" not in cta
    assert "获赠" not in cta
    assert "一杯茶" not in cta
    assert "comment" in cta.lower()


def test_recommendations_include_seed_patterns_when_library_empty():
    db = _db()
    svc = ScriptIntelligenceService()

    items = svc.recommendations(
        db,
        content_type="short_video",
        industry="home",
        platform="YouTube Shorts",
        goal="education",
        limit=3,
    )

    assert items
    assert items[0]["content_type"] == "short_video"
    assert items[0]["distilled_template"]["structure"]


def test_youtube_curiosity_reversal_seed_has_five_layers():
    db = _db()
    svc = ScriptIntelligenceService()

    items = svc.recommendations(db, content_type="youtube_long", limit=5)
    seed = next(
        (i for i in items if i["id"] == "seed_youtube_curiosity_reversal"), None
    )

    assert seed is not None, "curiosity-reversal archetype seed not returned"
    tpl = seed["distilled_template"]
    for layer in (
        "narrative_arc", "beat_skeletons", "signature_phrases",
        "full_skeleton", "topic_criteria", "fact_slots",
    ):
        assert tpl.get(layer), f"missing 5-layer key: {layer}"
    assert len(tpl["narrative_arc"]) == 7
    assert "curiosity_reversal" in seed["tags"]["structure"]


_SHORT_RAW = (
    "你知道装修时最容易被忽视的细节是什么吗？很多人关注墙面和地板，"
    "但灯光设计才是影响整体氛围的关键。评论告诉我你家最想改哪里。"
)


def test_recommendations_hides_public_seed_from_non_admin_shows_to_admin():
    """🔒 大杀招保护:核心公式库(public_seed)绝不下发给普通用户(防被刮);
    仅 admin(include_public=True)能在推荐里看到。别人的私有稿永远不漏。"""
    db = _db()
    svc = ScriptIntelligenceService()
    svc.create(db, {
        "title": "A自己的私有", "content_type": "short_video",
        "raw_text": _SHORT_RAW, "user_id": "userA",
        "scope": "private_raw", "quality_score": 9,
    })
    svc.create(db, {
        "title": "B私有泄露检测", "content_type": "short_video",
        "raw_text": _SHORT_RAW, "user_id": "userB",
        "scope": "private_raw", "quality_score": 9,
    })
    svc.create(db, {
        "title": "全局共享格式", "content_type": "short_video",
        "raw_text": _SHORT_RAW, "user_id": "admin",
        "scope": "public_seed", "quality_score": 9,
    })

    # 普通用户:只拿自己的稿;public_seed 核心库不下发;别人私有不漏。
    user_titles = [i["title"] for i in svc.recommendations(
        db, content_type="short_video", user_id="userA", limit=20)]
    assert "A自己的私有" in user_titles
    assert "全局共享格式" not in user_titles      # 🔒 核心公式库对普通用户隐藏
    assert "B私有泄露检测" not in user_titles

    # 运营(admin):能看到全局公式库(用于维护/验证),但仍不串别人私有。
    admin_titles = [i["title"] for i in svc.recommendations(
        db, content_type="short_video", user_id="userA", limit=20, include_public=True)]
    assert "全局共享格式" in admin_titles
    assert "A自己的私有" in admin_titles
    assert "B私有泄露检测" not in admin_titles


def test_random_template_matches_domain_and_refuses_unknown_domain_pool():
    """按领域智能匹配:动物视频抽到动物模板;无领域信号不再回整池随机。"""
    db = _db()
    svc = ScriptIntelligenceService()
    fin = svc.create(db, {"title": "财商-资产解读型", "content_type": "youtube_long",
                          "raw_text": _SHORT_RAW, "user_id": "admin",
                          "scope": "public_seed", "quality_score": 5,
                          "industry_tags": ["finance"]})
    ani = svc.create(db, {"title": "动物-怪物能力", "content_type": "youtube_long",
                          "raw_text": _SHORT_RAW, "user_id": "admin",
                          "scope": "public_seed", "quality_score": 5,
                          "industry_tags": ["animal"]})

    # 动物主题 → 永远抽到动物模板
    picks = {svc.random_template(db, "youtube_long", user_id="u1",
                                 context="黑曼巴蛇有多致命")["title"] for _ in range(15)}
    assert picks == {"动物-怪物能力"}, picks

    # 财商主题 → 永远抽到财商模板
    picks2 = {svc.random_template(db, "youtube_long", user_id="u1",
                                  context="巴菲特的投资逻辑和美股泡沫")["title"] for _ in range(15)}
    assert picks2 == {"财商-资产解读型"}, picks2

    # 无领域信号 → 不注入结构模板,避免整池随机导致领域串味
    assert svc.random_template(db, "youtube_long", user_id="u1",
                               context="随便讲点什么") is None


def test_domain_keywords_route_new_copy_topics_without_cross_domain_fallback():
    from backend.services.script_intelligence_service import detect_domains

    assert "animal" in detect_domains("蜜獾为什么被称为平头哥、天不怕地不怕")
    assert "animal" in detect_domains("章鱼为什么像来自外星的生物")
    assert "finance" in detect_domains("为什么很多人越努力反而越穷")
    assert "self_growth" in detect_domains("为什么很多人越努力反而越穷")
    assert "wisdom" in detect_domains("巴菲特和纳瓦尔最容易被误用的名人名言")
    assert "travel" in detect_domains("关西自由行京都大阪交通和票价攻略")


def test_random_template_new_copy_topics_stay_in_matched_domain():
    db = _db()
    svc = ScriptIntelligenceService()
    svc.create(db, {"title": "动物·怪物能力型", "content_type": "youtube_long",
                    "raw_text": _SHORT_RAW, "user_id": "admin", "scope": "public_seed",
                    "industry_tags": ["animal"], "quality_score": 5})
    svc.create(db, {"title": "财商·财富真相型", "content_type": "youtube_long",
                    "raw_text": _SHORT_RAW, "user_id": "admin", "scope": "public_seed",
                    "industry_tags": ["finance"], "quality_score": 5})
    svc.create(db, {"title": "老高频道·民俗灵异与都市传说类模板", "content_type": "youtube_long",
                    "raw_text": _SHORT_RAW, "user_id": "admin", "scope": "public_seed",
                    "industry_tags": ["mystery"], "quality_score": 5})
    svc.create(db, {"title": "Reisuistudio 冷水: 日本地方旅行祛魅类模板", "content_type": "youtube_long",
                    "raw_text": _SHORT_RAW, "user_id": "admin", "scope": "public_seed",
                    "industry_tags": ["history"], "quality_score": 5})

    animal_picks = {svc.random_template(db, "youtube_long", user_id="u1",
                                        context="蜜獾为什么被称为平头哥、天不怕地不怕")["title"]
                    for _ in range(20)}
    assert animal_picks == {"动物·怪物能力型"}

    finance_picks = {svc.random_template(db, "youtube_long", user_id="u1",
                                         context="为什么很多人越努力反而越穷")["title"]
                     for _ in range(20)}
    assert finance_picks == {"财商·财富真相型"}


def test_random_template_routes_to_best_fit_via_topic_criteria():
    """领域内选母模板:题目命中某模板的 适合 对象 → 优先抽它,不再整池纯随机。"""
    db = _db()
    svc = ScriptIntelligenceService()
    monster = svc.create(db, {"title": "动物·怪物能力型", "content_type": "youtube_long",
                              "raw_text": _SHORT_RAW, "user_id": "admin", "scope": "public_seed",
                              "industry_tags": ["animal"], "quality_score": 5})
    myth = svc.create(db, {"title": "动物·误解翻案型", "content_type": "youtube_long",
                           "raw_text": _SHORT_RAW, "user_id": "admin", "scope": "public_seed",
                           "industry_tags": ["animal"], "quality_score": 5})
    # 手动写入 topic_criteria(适合对象)
    monster.distilled_template = {"narrative_arc": ["a"], "topic_criteria": ["适合 蜂鸟、水熊虫、科莫多龙、捕鸟蛛"]}
    myth.distilled_template = {"narrative_arc": ["b"], "topic_criteria": ["适合 狮子、斑马、骆驼、丹顶鹤"]}
    db.commit()

    picks = {svc.random_template(db, "youtube_long", user_id="u1",
                                 context="水熊虫为什么几乎杀不死")["title"] for _ in range(15)}
    assert picks == {"动物·怪物能力型"}, picks  # 命中「水熊虫」→ 永远选怪物能力型


def test_random_template_gates_specific_frame_archetypes():
    """禁用模板门控:黑曼巴(无入侵/吃/宠物信号)绝不抽到入侵灾难型,只剩通用公式。"""
    db = _db()
    svc = ScriptIntelligenceService()
    for name in ("怪物能力型", "误解翻案型", "模板三：入侵灾难型", "餐桌活物型", "宠物真相型"):
        m = svc.create(db, {"title": f"动物·{name}", "content_type": "youtube_long",
                            "raw_text": _SHORT_RAW, "user_id": "admin", "scope": "public_seed",
                            "industry_tags": ["animal"], "quality_score": 5})
        m.distilled_template = {"narrative_arc": ["x"]}
    db.commit()
    picks = {svc.random_template(db, "youtube_long", user_id="u1",
                                 context="黑曼巴蛇到底有多致命")["title"] for _ in range(40)}
    assert "动物·模板三：入侵灾难型" not in picks
    assert "动物·餐桌活物型" not in picks
    assert "动物·宠物真相型" not in picks
    assert picks <= {"动物·怪物能力型", "动物·误解翻案型"}


def test_random_template_excludes_public_case_pool():
    """案例池(public_case)不进随机结构池:只有 public_seed 公式会被抽到。"""
    db = _db()
    svc = ScriptIntelligenceService()
    svc.create(db, {"title": "公式·入侵灾难型", "content_type": "youtube_long",
                    "raw_text": _SHORT_RAW, "user_id": "admin", "scope": "public_seed",
                    "industry_tags": ["animal"], "quality_score": 5})
    case = svc.create(db, {"title": "案例·负子蟾蒸馏卡", "content_type": "youtube_long",
                           "raw_text": _SHORT_RAW, "user_id": "admin", "scope": "public_case",
                           "industry_tags": ["animal"], "quality_score": 5})
    db.commit()
    picks = {svc.random_template(db, "youtube_long", user_id="u1",
                                 context="黑曼巴蛇有多致命")["title"] for _ in range(20)}
    assert "案例·负子蟾蒸馏卡" not in picks
    assert picks == {"公式·入侵灾难型"}




def test_create_manuscript_distills_and_recommends_custom_pattern():
    db = _db()
    svc = ScriptIntelligenceService()
    row = svc.create(db, {
        "title": "装修灯光误区",
        "content_type": "short_video",
        "user_id": "userZ",
        "raw_text": (
            "你知道装修时最容易被忽视的细节是什么吗？很多人关注墙面和地板，"
            "但灯光设计才是影响整体氛围的关键。评论告诉我你家最想改哪里。"
        ),
        "quality_score": 5,
    })

    # 用户拿到自己的稿(private_raw,走 user_id 过滤;public_seed 库不下发)。
    items = svc.recommendations(
        db,
        content_type="short_video",
        industry="home",
        platform="YouTube Shorts",
        goal="education",
        limit=1,
        user_id="userZ",
    )

    assert row.id == items[0]["id"]
    assert "home" in items[0]["tags"]["industry"]
