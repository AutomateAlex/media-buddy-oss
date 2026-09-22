from backend.services.short_video.visual_focus import repair_short_video_plan
from backend.services.literal_probe import _similar_anchor_terms


def test_repair_short_video_plan_recovers_chinese_fox_subject_from_generic_plan():
    plan = {
        "video_meta": {"motif_pool": ["documentary footage", "natural scene"]},
        "chunks": [{
            "chunk_index": 0,
            "sentence": "\u5728\u5317\u7f8e,\u72d0\u72f8\u4ee5\u667a\u6167\u548c\u9002\u5e94\u80fd\u529b\u8457\u79f0",
            "must_show": ["documentary"],
            "queries": [
                "documentary footage",
                "documentary close shot",
                "documentary natural scene",
            ],
        }],
    }

    repaired = repair_short_video_plan(
        plan,
        ["\u5728\u5317\u7f8e,\u72d0\u72f8\u4ee5\u667a\u6167\u548c\u9002\u5e94\u80fd\u529b\u8457\u79f0"],
        user_brief="\u72d0\u72f8\u79d1\u666e\u77ed\u89c6\u9891",
    )

    chunk = repaired["chunks"][0]
    assert repaired["video_meta"]["short_video_topic_anchor"] == "fox"
    assert chunk["visual_focus"] == "topic_subject"
    assert chunk["must_show"] == ["fox"]
    assert chunk["queries"][:3] == [
        "fox footage",
        "fox close shot",
        "fox natural scene",
    ]
    assert all("documentary" not in query.lower() for query in chunk["queries"])


def test_repair_short_video_plan_recovers_chinese_raven_subject_from_generic_plan():
    plan = {
        "video_meta": {"motif_pool": ["documentary concept visual"]},
        "chunks": [{
            "chunk_index": 0,
            "sentence": "\u4e4c\u9e26\u4e3a\u4ec0\u4e48\u80fd\u8bb0\u4f4f\u4eba\u7c7b\u7684\u8138?",
            "must_show": ["documentary"],
            "queries": ["documentary footage", "documentary close shot"],
        }],
    }

    repaired = repair_short_video_plan(
        plan,
        ["\u4e4c\u9e26\u4e3a\u4ec0\u4e48\u80fd\u8bb0\u4f4f\u4eba\u7c7b\u7684\u8138?"],
        user_brief="\u4e4c\u9e26\u51b7\u77e5\u8bc6",
    )

    chunk = repaired["chunks"][0]
    assert repaired["video_meta"]["short_video_topic_anchor"] == "raven"
    assert chunk["must_show"] == ["raven"]
    assert chunk["queries"][:3] == [
        "raven footage",
        "raven close shot",
        "raven natural scene",
    ]
    assert all("documentary" not in query.lower() for query in chunk["queries"])


def test_repair_short_video_plan_recovers_chinese_food_subject_from_generic_plan():
    plan = {
        "video_meta": {"motif_pool": ["documentary footage"]},
        "chunks": [{
            "chunk_index": 0,
            "sentence": "\u756a\u8304\u7092\u86cb\u4e3a\u4ec0\u4e48\u8981\u5148\u7092\u86cb?",
            "must_show": ["documentary"],
            "queries": ["documentary footage", "documentary close shot"],
        }],
    }

    repaired = repair_short_video_plan(
        plan,
        ["\u756a\u8304\u7092\u86cb\u4e3a\u4ec0\u4e48\u8981\u5148\u7092\u86cb?"],
        user_brief="\u7f8e\u98df\u6559\u7a0b\u77ed\u89c6\u9891",
    )

    chunk = repaired["chunks"][0]
    assert repaired["video_meta"]["short_video_topic_anchor"] == "tomato scrambled eggs"
    assert chunk["must_show"] == ["tomato scrambled eggs"]
    assert chunk["queries"][:3] == [
        "tomato scrambled eggs cooking",
        "tomato scrambled eggs close up",
        "tomato scrambled eggs food preparation",
    ]
    assert all("documentary" not in query.lower() for query in chunk["queries"])


def test_repair_short_video_plan_recovers_chinese_space_subject_from_generic_plan():
    plan = {
        "video_meta": {"motif_pool": ["abstract background"]},
        "chunks": [{
            "chunk_index": 0,
            "sentence": "\u9ed1\u6d1e\u4e3a\u4ec0\u4e48\u4f1a\u541e\u6389\u5468\u56f4\u7684\u5149?",
            "must_show": ["documentary"],
            "queries": ["documentary footage", "documentary close shot"],
        }],
    }

    repaired = repair_short_video_plan(
        plan,
        ["\u9ed1\u6d1e\u4e3a\u4ec0\u4e48\u4f1a\u541e\u6389\u5468\u56f4\u7684\u5149?"],
        user_brief="\u5b87\u5b99\u79d1\u5b66\u77ed\u89c6\u9891",
    )

    chunk = repaired["chunks"][0]
    assert repaired["video_meta"]["short_video_topic_anchor"] == "black hole"
    assert chunk["must_show"] == ["black hole"]
    assert chunk["queries"][:3] == [
        "black hole animation",
        "black hole space background",
        "black hole visualization",
    ]
    assert all("documentary" not in query.lower() for query in chunk["queries"])


def test_repair_short_video_plan_classifies_chinese_habitat_as_subject_safe_context():
    plan = {
        "video_meta": {"motif_pool": ["documentary footage", "natural scene"]},
        "chunks": [{
            "chunk_index": 0,
            "sentence": "\u4ece\u57ce\u5e02\u5230\u68ee\u6797\u3001\u4ece\u8349\u539f\u5230\u6c99\u6f20\uff0c\u90fd\u662f\u5b83\u4eec\u7684\u6816\u606f\u5730\u3002",
            "must_show": ["documentary"],
            "queries": ["documentary footage", "documentary natural scene"],
        }],
    }

    repaired = repair_short_video_plan(
        plan,
        ["\u4ece\u57ce\u5e02\u5230\u68ee\u6797\u3001\u4ece\u8349\u539f\u5230\u6c99\u6f20\uff0c\u90fd\u662f\u5b83\u4eec\u7684\u6816\u606f\u5730\u3002"],
        user_brief="\u72d0\u72f8\u79d1\u666e\u77ed\u89c6\u9891",
    )

    chunk = repaired["chunks"][0]
    assert chunk["visual_focus"] == "context_scene"
    assert chunk["queries"][0].startswith("fox ")
    assert "documentary" not in " ".join(chunk["queries"]).lower()


def test_repair_short_video_plan_keeps_chinese_cta_subject_safe():
    plan = {
        "video_meta": {"motif_pool": ["forest leaf macro veins", "wide landscape"]},
        "chunks": [{
            "chunk_index": 0,
            "sentence": "\u8bc4\u8bba\u5206\u4eab\u5427\uff01",
            "must_show": [],
            "queries": ["documentary footage"],
            "metaphor_queries": [],
            "sentence_type": "cta",
        }],
    }

    repaired = repair_short_video_plan(
        plan,
        ["\u8bc4\u8bba\u5206\u4eab\u5427\uff01"],
        user_brief="\u72d0\u72f8\u79d1\u666e\u77ed\u89c6\u9891",
    )

    chunk = repaired["chunks"][0]
    assert chunk["visual_focus"] == "transition"
    assert chunk["must_show"] == []
    assert chunk["queries"] == []
    assert chunk["metaphor_queries"][:3] == [
        "fox footage",
        "fox natural habitat",
        "fox close shot",
    ]


def test_repair_short_video_plan_builds_maritime_domain_pack_for_zheng_he():
    sentence = "\u90d1\u548c\u7684\u8fdc\u822a\u582a\u79f0\u58ee\u4e3e\uff0c1405\u5e74\u4ed6\u9996\u6b21\u51fa\u6d77\u3002"
    plan = {
        "video_meta": {"motif_pool": ["forest leaf macro veins", "river delta aerial"]},
        "chunks": [{
            "chunk_index": 0,
            "sentence": sentence,
            "must_show": ["ancient history"],
            "queries": ["ancient historical scene", "data visualization"],
        }],
    }

    repaired = repair_short_video_plan(plan, [sentence], user_brief="\u90d1\u548c\u4e0b\u897f\u6d0b\u77ed\u89c6\u9891")

    chunk = repaired["chunks"][0]
    joined = " ".join(chunk["queries"]).lower()
    assert repaired["video_meta"]["domain"] == "history_maritime"
    assert any(term in joined for term in ["ship", "sailing", "fleet", "harbor", "ocean", "maritime"])
    assert "forest leaf macro veins" not in joined
    assert "data visualization" not in joined
    assert chunk["query_source"] == "domain_visual_pack"
    assert chunk["query_degradation_level"] == "domain_repaired"


def test_repair_short_video_plan_strengthens_weak_street_must_show():
    sentence = "一个卖甘蔗汁的小贩"
    plan = {
        "video_meta": {
            "motif_pool": ["Mumbai street wide shot crowded sidewalk"],
        },
        "chunks": [{
            "chunk_index": 0,
            "sentence": sentence,
            "must_show": ["street"],
            "queries": [
                "Indian street vendor crushing sugarcane",
                "Mumbai juice seller with manual press",
                "South Asian man serving fresh juice from cart",
            ],
        }],
    }

    repaired = repair_short_video_plan(plan, [sentence], user_brief="孟买街头甘蔗汁小贩")

    must_show = repaired["chunks"][0]["must_show"]
    assert repaired["video_meta"]["domain"] != "technology_explainer"
    assert "street" not in must_show
    assert "tree up" not in must_show
    assert any(term in must_show for term in ["vendor", "sugarcane", "juice"])


def test_repair_short_video_plan_does_not_promote_color_to_hard_anchor():
    sentence = "Mumbai street vendor runs a rusty blue drink cart."
    plan = {
        "video_meta": {
            "motif_pool": ["mumbai street food vendor", "rusty blue street food cart mumbai"],
        },
        "chunks": [{
            "chunk_index": 0,
            "sentence": sentence,
            "must_show": ["blue"],
            "queries": [
                "mumbai blue footage",
                "rusty blue street food cart mumbai",
                "blue indian street vendor cart close up",
            ],
        }],
    }

    repaired = repair_short_video_plan(
        plan,
        [sentence],
        user_brief="Mumbai street drink vendor business story",
        domain_pack={
            "domain": "mumbai_street_drink_business",
            "topic_anchor": "mumbai street drink vendor",
            "domain_visual_pack": [
                "mumbai street food vendor",
                "indian street drink cart",
                "customers waiting in line",
                "hands counting rupees",
            ],
            "domain_motif_pool": [
                "mumbai street food vendor",
                "indian street drink cart",
            ],
            "forbidden_visual_pack": ["beer", "train station"],
        },
    )

    must_show = repaired["chunks"][0]["must_show"]
    assert "blue" not in must_show
    assert "rusty" not in must_show
    assert any(
        any(word in term for word in ["food", "cart", "vendor", "drink"])
        for term in must_show
    )


def test_repair_short_video_plan_does_not_promote_empty_state_to_story_subject(monkeypatch):
    monkeypatch.setenv("ENABLE_CONTENT_DOMAIN_PACKS", "0")
    sentences = [
        "为什么别家便利店货架空了三个月",
        "冰柜还堆着二十箱可乐",
        "去年十月起他专挑临期90天的酸奶进货",
        "每箱便宜三块七保质期剩27天",
        "他把货架分成三区左区放临期食品贴黄标",
        "黄标中区是当日鲜奶用冰袋压着",
        "右区永远空着两格等突发需求",
        "四千二全来自损耗率压到1.3%",
    ]
    plan = {
        "video_meta": {
            "motif_pool": ["empty footage", "empty natural background"],
        },
        "chunks": [
            {
                "chunk_index": idx,
                "sentence": sentence,
                "must_show": ["empty"],
                "queries": [
                    "empty footage",
                    "empty close shot",
                    "empty natural scene",
                    "refrigerated display case" if idx == 1 else "convenience store shelf",
                ],
                "metaphor_queries": ["empty environment"],
            }
            for idx, sentence in enumerate(sentences)
        ],
    }

    repaired = repair_short_video_plan(
        plan,
        sentences,
        user_brief="便利店临期食品管理",
        domain_pack={
            "domain": "retail_convenience_store",
            "topic_anchor": "convenience store shelf",
            "domain_visual_pack": [
                "convenience store shelf",
                "refrigerated display case",
                "packaged food aisle",
                "yellow discount tag",
            ],
            "domain_motif_pool": [
                "convenience store shelf",
                "packaged food aisle",
                "store inventory shelf",
            ],
            "forbidden_visual_pack": [
                "cinema seats",
                "subway train",
                "swing set",
                "broken glass",
            ],
        },
    )

    assert repaired["video_meta"]["short_video_topic_anchor"] == "convenience store shelf"
    all_terms = " ".join(
        " ".join(str(x) for x in chunk.get(field, []))
        for chunk in repaired["chunks"]
        for field in ("must_show", "queries", "metaphor_queries")
    ).lower()
    assert "empty footage" not in all_terms
    assert "empty close shot" not in all_terms
    assert "empty natural scene" not in all_terms
    assert "empty environment" not in all_terms
    assert all(chunk["must_show"] != ["empty"] for chunk in repaired["chunks"])

    assert repaired["chunks"][1]["must_show"][:2] == [
        "refrigerated display case",
        "cola bottles",
    ]
    assert "yogurt cups" in repaired["chunks"][2]["must_show"]
    assert "date label" in repaired["chunks"][3]["must_show"]
    assert "store shelf" in repaired["chunks"][4]["must_show"]
    assert "refrigerated dairy shelf" in repaired["chunks"][5]["must_show"]
    assert "retail shelf" in repaired["chunks"][6]["must_show"]
    assert "partly empty retail shelf with packaged goods" in repaired["chunks"][6]["queries"]
    assert repaired["chunks"][7]["story_soft_fallback"] is True


def test_retail_profit_price_sentences_prefer_products_not_cash(monkeypatch):
    monkeypatch.setenv("ENABLE_CONTENT_DOMAIN_PACKS", "0")
    sentences = [
        "本地麻花保质期短，但毛利高18%",
        "每箱便宜三块七，保质期剩27天时才上架。",
    ]
    plan = {
        "video_meta": {"motif_pool": ["convenience store shelf"]},
        "chunks": [
            {
                "chunk_index": 0,
                "sentence": sentences[0],
                "must_show": ["cash"],
                "queries": ["hands counting cash money", "stack of gold coins"],
            },
            {
                "chunk_index": 1,
                "sentence": sentences[1],
                "must_show": ["cash"],
                "queries": ["money banknotes close up", "expiry date label on packaged food"],
            },
        ],
    }

    repaired = repair_short_video_plan(
        plan,
        sentences,
        user_brief="便利店临期食品经营",
        domain_pack={
            "domain": "retail_convenience_store",
            "topic_anchor": "convenience store shelf",
            "domain_visual_pack": ["convenience store shelf", "packaged food aisle"],
            "domain_motif_pool": ["convenience store shelf", "packaged food aisle"],
            "forbidden_visual_pack": [],
        },
    )

    snack_chunk, expiry_chunk = repaired["chunks"]
    all_queries = " ".join(
        " ".join(str(x) for x in chunk.get("queries", []))
        for chunk in repaired["chunks"]
    ).lower()
    assert "cash" not in snack_chunk["must_show"]
    assert "cash" not in expiry_chunk["must_show"]
    assert "hands counting cash" not in all_queries
    assert "gold coins" not in all_queries
    assert "money banknotes" not in all_queries
    assert snack_chunk["queries"][0] == "packaged fried snack on convenience store shelf"
    assert "packaged fried snack" in snack_chunk["must_show"]
    assert snack_chunk["story_soft_fallback"] is False
    assert "potatoes" in " ".join(snack_chunk["forbidden_visual_pack"]).lower()
    assert "date label" in expiry_chunk["must_show"]


def test_retail_empty_shelf_forbids_bookshelf_and_abstract_world(monkeypatch):
    monkeypatch.setenv("ENABLE_CONTENT_DOMAIN_PACKS", "0")
    sentence = "右区永远空着两格，等突发需求和当天补货。"
    plan = {
        "video_meta": {"motif_pool": ["convenience store shelf"]},
        "chunks": [{
            "chunk_index": 0,
            "sentence": sentence,
            "must_show": ["empty"],
            "queries": ["empty shelf space in convenience store"],
        }],
    }

    repaired = repair_short_video_plan(
        plan,
        [sentence],
        user_brief="便利店临期食品经营",
        domain_pack={
            "domain": "retail_convenience_store",
            "topic_anchor": "convenience store shelf",
            "domain_visual_pack": ["convenience store shelf"],
            "domain_motif_pool": ["convenience store shelf"],
            "forbidden_visual_pack": [],
        },
    )

    chunk = repaired["chunks"][0]
    forbidden = " ".join(chunk["forbidden_visual_pack"]).lower()
    assert chunk["must_show"] == ["retail shelf"]
    assert "space" not in chunk["must_show"]
    assert "bookshelf" in forbidden
    assert "abstract ink" in forbidden
    assert "liquid paint abstract" in forbidden
    assert "fully stocked shelf" in forbidden


def test_repair_short_video_plan_prunes_forbidden_terms_needed_by_story():
    sentence = "\u6bcf\u676f\u73b0\u91d1\u5229\u6da6\u589e\u957f\uff0c\u589e\u957f\u56fe\u5f88\u660e\u663e\u3002"
    plan = {
        "video_meta": {"motif_pool": ["Mumbai sugarcane juice stall"]},
        "chunks": [{
            "chunk_index": 0,
            "sentence": sentence,
            "must_show": ["profit"],
            "queries": ["hands counting cash money", "business graph arrow going up"],
        }],
    }

    repaired = repair_short_video_plan(
        plan,
        [sentence],
        user_brief="\u5b5f\u4e70\u7518\u8517\u6c41\u644a\u4e3b\u73b0\u91d1\u5229\u6da6\u589e\u957f\u56fe",
        domain_pack={
            "domain": "street_food_mumbai",
            "topic_anchor": "mumbai sugarcane juice vendor",
            "domain_visual_pack": ["mumbai sugarcane juice stall"],
            "domain_motif_pool": ["mumbai sugarcane juice stall"],
            "forbidden_visual_pack": [
                "money transactions",
                "charts and graphs",
                "data visualization",
                "taiwan metro station",
            ],
        },
    )

    forbidden = repaired["video_meta"]["forbidden_visual_pack"]
    assert "money transactions" not in forbidden
    assert "charts and graphs" not in forbidden
    assert "data visualization" not in forbidden
    assert "taiwan metro station" in forbidden


def test_repair_short_video_plan_scopes_must_show_to_each_sentence():
    sentences = [
        "在孟买街头，一个卖糖水的小贩",
        "他把铜壶换成厚壁不锈钢壶，调整糖浆浓度",
        "顾客排队变长，新定价18卢比，复购率升到67%。",
    ]
    plan = {
        "video_meta": {
            "motif_pool": [
                "busy street scene",
                "smiling customers",
                "hands exchanging money",
                "vendors preparing drinks",
            ],
        },
        "chunks": [
            {
                "chunk_index": 0,
                "sentence": sentences[0],
                "must_show": ["queue"],
                "queries": ["customers waiting in line"],
            },
            {
                "chunk_index": 1,
                "sentence": sentences[1],
                "must_show": ["queue"],
                "queries": ["customers waiting in line"],
            },
            {
                "chunk_index": 2,
                "sentence": sentences[2],
                "must_show": ["cash"],
                "queries": ["hands counting cash money"],
            },
        ],
    }

    repaired = repair_short_video_plan(
        plan,
        sentences,
        user_brief="孟买糖水摊通过调整壶和糖浆提升复购",
        domain_pack={
            "domain": "mumbai_street_drink_business",
            "topic_anchor": "mumbai street drink vendor",
            "domain_visual_pack": [
                "mumbai street drink vendor",
                "street vendor pouring sugar syrup drink",
                "customers waiting in line",
                "hands counting rupees",
            ],
            "domain_motif_pool": [
                "mumbai street drink vendor",
                "street vendor preparing drink",
                "customers waiting in line",
            ],
            "forbidden_visual_pack": ["beer", "train station"],
        },
    )

    chunks = repaired["chunks"]
    assert "queue" not in chunks[0]["must_show"]
    assert any(term in chunks[0]["must_show"] for term in ["vendor", "drink preparation"])
    assert chunks[1]["must_show"][:2] == ["kettle", "syrup or honey"]
    assert chunks[2]["must_show"][0] == "customer line"
    assert "cash" not in chunks[2]["must_show"]


def test_repair_short_video_plan_treats_bare_cup_as_unit_not_object():
    sentences = ["每杯卖15卢比，成本8卢比", "日均卖200杯。", "准备杯子和补冰块。"]
    plan = {
        "video_meta": {"motif_pool": ["mumbai street drink vendor"]},
        "chunks": [
            {"chunk_index": 0, "sentence": sentences[0], "must_show": ["cup"], "queries": ["cup close up"]},
            {"chunk_index": 1, "sentence": sentences[1], "must_show": ["cup"], "queries": ["cup close up"]},
            {"chunk_index": 2, "sentence": sentences[2], "must_show": ["cup"], "queries": ["cup close up"]},
        ],
    }

    repaired = repair_short_video_plan(
        plan,
        sentences,
        user_brief="孟买糖水摊销量和备杯",
        domain_pack={
            "domain": "mumbai_street_drink_business",
            "topic_anchor": "mumbai street drink vendor",
            "domain_visual_pack": ["mumbai street drink vendor"],
            "domain_motif_pool": ["mumbai street drink vendor"],
            "forbidden_visual_pack": [],
        },
    )

    assert "cup" not in repaired["chunks"][0]["must_show"]
    assert "cup" not in repaired["chunks"][1]["must_show"]
    assert "cup" in repaired["chunks"][2]["must_show"]
    assert repaired["chunks"][1]["story_soft_fallback"] is True
    assert repaired["chunks"][2]["story_soft_fallback"] is False


def test_repair_short_video_plan_marks_compound_transaction_as_soft_fallback():
    sentence = "摊主倒饮品，印度卢比收钱，"
    plan = {
        "video_meta": {"motif_pool": ["mumbai street drink vendor"]},
        "chunks": [{
            "chunk_index": 0,
            "sentence": sentence,
            "must_show": ["drink preparation", "vendor", "cash"],
            "queries": ["street vendor pouring drink and taking cash"],
        }],
    }

    repaired = repair_short_video_plan(
        plan,
        [sentence],
        user_brief="孟买糖水摊交易流程",
        domain_pack={
            "domain": "mumbai_street_drink_business",
            "topic_anchor": "mumbai street drink vendor",
            "domain_visual_pack": ["mumbai street drink vendor", "hands counting rupees"],
            "domain_motif_pool": ["mumbai street drink vendor"],
            "forbidden_visual_pack": [],
        },
    )

    chunk = repaired["chunks"][0]
    assert chunk["story_soft_fallback"] is True
    assert "cash" in chunk["must_show"]


def test_repair_short_video_plan_builds_mayan_domain_pack_for_ruins():
    sentence = "\u739b\u96c5\u9057\u8ff9\u7684\u795e\u5e99\u548c\u77f3\u523b\u85cf\u7740\u53e4\u6587\u660e\u7684\u7ebf\u7d22\u3002"
    plan = {
        "video_meta": {"motif_pool": ["documentary footage"]},
        "chunks": [{
            "chunk_index": 0,
            "sentence": sentence,
            "must_show": ["ancient history"],
            "queries": ["ancient historical scene", "river delta aerial"],
        }],
    }

    repaired = repair_short_video_plan(plan, [sentence], user_brief="\u739b\u96c5\u9057\u8ff9\u79d1\u666e")

    chunk = repaired["chunks"][0]
    joined = " ".join(chunk["queries"]).lower()
    assert repaired["video_meta"]["domain"] == "ancient_civilization"
    assert any(term in joined for term in ["mayan", "ruins", "temple", "archaeological", "stone"])
    assert "river delta aerial" not in joined
    assert "sailing ship" not in joined


def test_repair_short_video_plan_builds_weather_domain_pack_for_cold_wave():
    sentences = [
        "\u6781\u5bd2\u7684\u5bd2\u6f6e\u5e2d\u5377\u800c\u6765\uff0c\u80cc\u540e\u9690\u85cf\u7740\u4e0d\u4e3a\u4eba\u77e5\u7684\u771f\u76f8\u3002",
        "\u79d1\u5b66\u5bb6\u53d1\u73b0\uff0c\u5f53\u5317\u6781\u53d8\u6696\uff0c\u66f4\u591a\u51b7\u7a7a\u6c14\u4f1a\u5411\u5357\u6269\u6563\u3002",
        "\u6d77\u6d0b\u7684\u6e29\u5ea6\u4e5f\u5728\u6084\u7136\u53d8\u5316\uff0c\u5bfc\u81f4\u6d77\u6d0b\u6c14\u5019\u6a21\u5f0f\u6301\u7eed\u6ce2\u52a8\u3002",
        "\u5bd2\u6f6e\u7684\u5230\u6765\uff0c\u5f80\u5f80\u4f34\u968f\u5f3a\u70c8\u7684\u98ce\u66b4\u548c\u964d\u96ea\u3002",
    ]
    plan = {
        "video_meta": {"motif_pool": ["volcano footage", "scientific research on climate"]},
        "chunks": [
            {
                "chunk_index": idx,
                "sentence": sentence,
                "must_show": ["scientific research"],
                "queries": ["volcano footage", "scientific research on climate", "polar bear habitats"],
            }
            for idx, sentence in enumerate(sentences)
        ],
    }

    repaired = repair_short_video_plan(plan, sentences, user_brief="\u5bd2\u6f6e\u548c\u6781\u7aef\u5929\u6c14\u77ed\u89c6\u9891")

    all_queries = " ".join(
        q
        for chunk in repaired["chunks"]
        for q in list(chunk["queries"]) + list(chunk["metaphor_queries"]) + list(chunk["must_show"])
    ).lower()
    assert repaired["video_meta"]["domain"] == "weather_climate"
    assert any(
        marker in all_queries
        for marker in [
            "cold wave",
            "winter storm",
            "snowstorm",
            "arctic",
            "ocean temperature",
            "weather satellite",
        ]
    )
    for bad in [
        "scientific research",
        "laboratory",
        "microscope",
        "chemistry",
        "volcano",
        "lava",
        "polar bear",
    ]:
        assert bad not in all_queries
    scientist_chunk = repaired["chunks"][1]
    scientist_queries = " ".join(scientist_chunk["queries"]).lower()
    assert "weather satellite" in scientist_queries or "arctic" in scientist_queries
    ocean_chunk = repaired["chunks"][2]
    assert "ocean temperature" in " ".join(ocean_chunk["queries"]).lower()


def test_repair_short_video_plan_prefers_mesopotamia_rule_over_malformed_domain():
    sentences = [
        "\u4f60\u77e5\u9053\u82cf\u7f8e\u5c14\u6587\u660e\u4e3a\u4ec0\u4e48\u88ab\u79f0\u4e3a\u4eba\u7c7b\u6700\u65e9\u7684\u57ce\u5e02\u6587\u660e\u5417\uff1f",
        "\u4ed6\u4eec\u7684\u6954\u5f62\u6587\u5b57\u5199\u5728\u6ce5\u677f\u4e0a\uff0c\u4fdd\u7559\u4e86\u53e4\u8001\u7684\u8bb0\u5f55\u3002",
    ]
    plan = {
        "video_meta": {"motif_pool": ["archaeological footage", "ancient architecture"]},
        "chunks": [{
            "chunk_index": 0,
            "sentence": sentences[0],
            "must_show": ["ancient civilizations footage"],
            "queries": ["ancient historical scene", "forest leaf macro veins"],
        }, {
            "chunk_index": 1,
            "sentence": sentences[1],
            "must_show": ["ancient civilizations footage"],
            "queries": ["ancient ancient architecture", "data visualization"],
        }],
    }
    malformed_pack = {
        "domain": "ancientcivilizationsfootage.com",
        "topic_anchor": "ancient civilizations footage",
        "domain_visual_pack": ["archaeological footage", "ancient architecture"],
        "domain_motif_pool": ["archaeological footage"],
        "forbidden_visual_pack": [],
    }

    repaired = repair_short_video_plan(
        plan,
        sentences,
        user_brief="\u82cf\u7f8e\u5c14\u6587\u660e\u51b7\u77e5\u8bc6",
        domain_pack=malformed_pack,
    )

    joined = " ".join(q for c in repaired["chunks"] for q in c["queries"]).lower()
    assert repaired["video_meta"]["domain"] == "ancient_mesopotamia"
    assert any(term in joined for term in ["mesopotamian", "ziggurat", "cuneiform", "clay tablet"])
    assert "ancientcivilizationsfootage" not in joined
    assert "forest leaf macro veins" not in joined
    assert "data visualization" not in joined
    assert all(c["query_source"] == "domain_visual_pack" for c in repaired["chunks"])


def test_repair_short_video_plan_does_not_match_ur_inside_turkish():
    from backend.services.short_video.visual_focus import repair_short_video_plan

    sentence = (
        "In Istanbul, a Turkish chestnut vendor roasts chestnuts while "
        "customers wait beside the cart."
    )
    plan = {
        "video_meta": {"motif_pool": ["Istanbul street food stall"]},
        "chunks": [{
            "chunk_index": 0,
            "sentence": sentence,
            "must_show": ["chestnut vendor"],
            "queries": ["Istanbul chestnut vendor", "Turkish street food cart"],
        }],
    }

    repaired = repair_short_video_plan(
        plan,
        [sentence],
        user_brief="Turkish street food chestnut story",
    )

    joined = " ".join(q for c in repaired["chunks"] for q in c["queries"]).lower()
    assert repaired["video_meta"]["domain"] != "ancient_mesopotamia"
    assert "mesopotamian" not in joined
    assert "ziggurat" not in joined
    assert "cuneiform" not in joined


def test_repair_short_video_plan_keeps_vietnamese_coffee_out_of_mesopotamia():
    from backend.services.short_video.visual_focus import repair_short_video_plan

    sentence = (
        "A Vietnamese phin coffee stall in Hanoi sells iced coffee for "
        "18000 dong before morning commuters arrive."
    )
    plan = {
        "video_meta": {"motif_pool": ["Hanoi street coffee stall"]},
        "chunks": [{
            "chunk_index": 0,
            "sentence": sentence,
            "must_show": ["phin coffee"],
            "queries": ["Vietnamese phin coffee stall", "Hanoi street coffee vendor"],
        }],
    }

    repaired = repair_short_video_plan(
        plan,
        [sentence],
        user_brief="Vietnamese street coffee story",
    )

    joined = " ".join(q for c in repaired["chunks"] for q in c["queries"]).lower()
    assert repaired["video_meta"]["domain"] != "ancient_mesopotamia"
    assert "mesopotamian" not in joined
    assert "ziggurat" not in joined
    assert "cuneiform" not in joined


def test_sentence_local_must_show_prefers_object_over_generic_vendor():
    from backend.services.short_video.visual_focus import _sentence_local_must_show

    must_show = _sentence_local_must_show(
        "The vendor makes small slits in the chestnuts.",
        ["turkish vendor cutting chestnuts slits hands", "turkish vendor slicing chestnuts close up"],
        ["vendor"],
    )

    assert must_show[0] == "chestnut"
    assert "vendor" not in must_show


def test_sentence_local_must_show_prefers_story_objects_for_dynamic_foods():
    from backend.services.short_video.visual_focus import _sentence_local_must_show

    coffee = _sentence_local_must_show(
        "Ice is added before the dark coffee is stirred in.",
        ["ice added dark coffee stirred", "iced milk coffee preparation"],
        ["ice", "stirring"],
    )
    chestnut = _sentence_local_must_show(
        "At dusk in Istanbul, a Turkish roasted chestnut cart glows on the market street.",
        ["turkish roasted chestnut street vendor istanbul"],
        ["istanbul", "dusk"],
    )
    street_scene = _sentence_local_must_show(
        "Motorbike commuters wait near the curb.",
        ["vietnamese coffee stall", "coffee close up"],
        ["coffee"],
    )

    assert coffee[0] == "coffee"
    assert "ice" not in coffee
    assert chestnut == ["chestnut"]
    assert street_scene == []


def test_repair_short_video_plan_uses_archaeology_for_mesopotamian_hunter_gatherer_sentence():
    sentence = "\u66f4\u65e9\u7684\u72e9\u730e\u91c7\u96c6\u8005\u5728\u4e24\u6cb3\u6d41\u57df\u751f\u5b58\u4e86\u6570\u5343\u5e74\u3002"
    plan = {
        "video_meta": {"motif_pool": ["documentary footage"]},
        "chunks": [{
            "chunk_index": 0,
            "sentence": sentence,
            "must_show": ["documentary"],
            "queries": ["documentary footage", "documentary natural scene"],
        }],
    }

    repaired = repair_short_video_plan(plan, [sentence], user_brief="\u82cf\u7f8e\u5c14\u6587\u660e")

    joined = " ".join(repaired["chunks"][0]["queries"]).lower()
    metaphor_joined = " ".join(repaired["chunks"][0]["metaphor_queries"]).lower()
    must_show_joined = " ".join(repaired["chunks"][0]["must_show"]).lower()
    must_not_joined = " ".join(repaired["chunks"][0]["must_not_show"]).lower()
    similar_anchors = " ".join(_similar_anchor_terms(repaired["chunks"][0])).lower()
    assert repaired["video_meta"]["domain"] == "ancient_mesopotamia"
    assert any(term in joined for term in ["hunter gatherer", "stone tools", "archaeological excavation"])
    assert "documentary" not in joined
    assert "forest landscape" not in joined
    assert "wild footage" not in joined
    assert "wild hunting" not in joined
    assert "natural habitat" not in joined
    assert "wild animal hunting" not in joined
    assert "wild" not in must_show_joined
    assert "animal" not in must_show_joined
    assert "hunting" not in must_show_joined
    assert "wild" not in similar_anchors
    assert "animal" not in similar_anchors
    assert "hunting" not in similar_anchors
    assert "wild footage" not in metaphor_joined
    assert "wild hunting" not in metaphor_joined
    assert "wild animal hunting" not in metaphor_joined
    assert "wild footage" in must_not_joined
    assert "wild hunting" in must_not_joined
    assert "wild animal hunting" in must_not_joined


def _degraded_default_plan(sentence: str) -> dict:
    return {
        "video_meta": {
            "motif_pool": [
                "forest leaf macro veins",
                "river delta aerial top view",
                "crystal growth time lapse",
            ],
            "motif_pool_source": "default_fallback",
        },
        "chunks": [{
            "chunk_index": 0,
            "sentence": sentence,
            "must_show": ["forest leaf macro veins"],
            "queries": ["forest leaf macro veins"],
            "plan_source": "deterministic_fallback",
        }],
        "meta": {
            "director_degraded": True,
            "degradation_reasons": [
                "deterministic_chunk_fallback",
                "default_motif_pool",
            ],
        },
    }


def test_degraded_default_pool_cannot_hijack_raven_topic_anchor():
    sentence = "\u4e4c\u9e26\u4e3a\u4ec0\u4e48\u4f1a\u4f7f\u7528\u5de5\u5177?"
    repaired = repair_short_video_plan(
        _degraded_default_plan(sentence),
        [sentence],
        user_brief="\u4e4c\u9e26\u7684\u5de5\u5177\u667a\u80fd",
    )

    video_meta = repaired["video_meta"]
    chunk = repaired["chunks"][0]
    joined = " ".join(chunk["queries"]).lower()
    assert video_meta["short_video_topic_anchor"] == "raven"
    assert video_meta["topic_anchor_source"] == "brief_dictionary"
    assert video_meta["default_motif_pool_quarantined"] is True
    assert video_meta["director_degraded"] is True
    assert "raven" in joined
    assert "forest leaf macro veins" not in joined
    assert "river delta" not in joined


def test_degraded_default_pool_recovers_bear_without_topic_specific_patch():
    sentence = "\u68d5\u718a\u5982\u4f55\u5728\u51ac\u5929\u524d\u50a8\u5b58\u80fd\u91cf?"
    repaired = repair_short_video_plan(
        _degraded_default_plan(sentence),
        [sentence],
        user_brief="\u68d5\u718a\u751f\u5b58\u6545\u4e8b",
    )

    joined = " ".join(repaired["chunks"][0]["queries"]).lower()
    assert repaired["video_meta"]["short_video_topic_anchor"] == "bear"
    assert "bear" in joined
    assert "forest leaf macro veins" not in joined


def test_degraded_finance_story_uses_independent_domain_contract():
    sentence = "\u666e\u901a\u5bb6\u5ead\u4e3a\u4ec0\u4e48\u603b\u662f\u5b58\u4e0d\u4e0b\u94b1?"
    domain_pack = {
        "domain": "finance_economy",
        "topic_anchor": "household cash flow",
        "domain_visual_pack": [
            "family reviewing monthly bills",
            "household budget spreadsheet",
            "banking app balance close up",
        ],
        "domain_motif_pool": [
            "family reviewing monthly bills",
            "household budget spreadsheet",
            "banking app balance close up",
        ],
        "forbidden_visual_pack": ["wild animal", "forest leaf macro veins"],
    }
    repaired = repair_short_video_plan(
        _degraded_default_plan(sentence),
        [sentence],
        user_brief="\u666e\u901a\u4eba\u7684\u5bb6\u5ead\u8d22\u52a1",
        domain_pack=domain_pack,
    )

    chunk = repaired["chunks"][0]
    joined = " ".join(chunk["queries"]).lower()
    assert repaired["video_meta"]["short_video_topic_anchor"] == "household cash flow"
    assert repaired["video_meta"]["topic_anchor_source"] == "domain_pack"
    assert chunk["query_source"] == "domain_visual_pack"
    assert any(term in joined for term in ("budget", "bills", "banking"))
    assert "natural habitat" not in joined
    assert "forest leaf macro veins" not in joined


def test_degraded_finance_story_has_deterministic_domain_fallback():
    sentence = "\u901a\u8d27\u81a8\u80c0\u4e3a\u4ec0\u4e48\u4f1a\u5077\u8d70\u666e\u901a\u4eba\u7684\u8d2d\u4e70\u529b?"
    repaired = repair_short_video_plan(
        _degraded_default_plan(sentence),
        [sentence],
        user_brief="\u666e\u901a\u4eba\u7684\u91d1\u94b1\u4e0e\u8d22\u5546\u6545\u4e8b",
    )

    joined = " ".join(repaired["chunks"][0]["queries"]).lower()
    assert repaired["video_meta"]["domain"] == "finance_economy"
    assert repaired["video_meta"]["short_video_topic_anchor"] == "personal finance"
    assert any(term in joined for term in ("budget", "bills", "banking", "financial"))
    assert "natural habitat" not in joined
    assert "forest leaf macro veins" not in joined


def test_degraded_biography_story_uses_independent_domain_contract():
    sentence = "\u8fd9\u4f4d\u521b\u59cb\u4eba\u7684\u7b2c\u4e00\u6b21\u521b\u4e1a\u5e76\u4e0d\u6210\u529f\u3002"
    repaired = repair_short_video_plan(
        _degraded_default_plan(sentence),
        [sentence],
        user_brief="\u4e00\u4f4d\u4f01\u4e1a\u5bb6\u7684\u4eba\u7269\u4f20\u8bb0",
    )

    chunk = repaired["chunks"][0]
    joined = " ".join(chunk["queries"]).lower()
    assert repaired["video_meta"]["domain"] == "biography_people"
    assert any(term in joined for term in ("portrait", "career", "work", "archive"))
    assert "natural habitat" not in joined
    assert "forest leaf macro veins" not in joined


def test_biography_priority_beats_incidental_company_marker():
    sentence = "\u8fd9\u4f4d\u521b\u59cb\u4eba\u7684\u7b2c\u4e00\u5bb6\u516c\u53f8\u5931\u8d25\u540e\uff0c\u4ed6\u91cd\u65b0\u89c4\u5212\u4ea7\u54c1\u3002"
    repaired = repair_short_video_plan(
        _degraded_default_plan(sentence),
        [sentence],
        user_brief="\u4e00\u4f4d\u4f01\u4e1a\u5bb6\u4ece\u521b\u4e1a\u5931\u8d25\u5230\u91cd\u65b0\u5f00\u59cb\u7684\u4eba\u7269\u4f20\u8bb0",
    )

    joined = " ".join(repaired["chunks"][0]["queries"]).lower()
    assert repaired["video_meta"]["domain"] == "biography_people"
    assert repaired["video_meta"]["short_video_topic_anchor"] == "biography story"
    assert any(term in joined for term in ("portrait", "career", "work", "archive"))
    assert "budget" not in joined
    assert "bills" not in joined


def test_finance_story_with_entrepreneur_but_no_biography_signal_stays_finance():
    sentence = "\u4f01\u4e1a\u5bb6\u5fc5\u987b\u7ba1\u7406\u516c\u53f8\u503a\u52a1\u548c\u6bcf\u6708\u9884\u7b97\u3002"
    repaired = repair_short_video_plan(
        _degraded_default_plan(sentence),
        [sentence],
        user_brief="\u5c0f\u4f01\u4e1a\u8d22\u52a1\u4e0e\u73b0\u91d1\u6d41",
    )

    joined = " ".join(repaired["chunks"][0]["queries"]).lower()
    assert repaired["video_meta"]["domain"] == "finance_economy"
    assert any(term in joined for term in ("budget", "bills", "banking", "financial"))


def test_healthy_director_can_legitimately_request_river_delta_footage():
    sentence = "A river delta splits into visible channels before reaching the sea."
    plan = {
        "video_meta": {
            "motif_pool": ["river delta aerial top view"],
            "motif_pool_source": "llm",
        },
        "chunks": [{
            "chunk_index": 0,
            "sentence": sentence,
            "must_show": ["river delta"],
            "queries": ["river delta aerial top view"],
            "plan_source": "llm_expand",
        }],
        "meta": {"director_degraded": False, "degradation_reasons": []},
    }
    repaired = repair_short_video_plan(
        plan,
        [sentence],
        user_brief="river delta geography",
    )

    assert repaired["video_meta"]["default_motif_pool_quarantined"] is False
    assert repaired["chunks"][0]["queries"][0] == "river delta aerial top view"
    assert repaired["chunks"][0]["query_source"] == "llm_native"
