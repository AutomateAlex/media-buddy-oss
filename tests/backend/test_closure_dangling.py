"""断头列表收口检测 —— 复现「开了三个条件只讲第一」的漏判。"""
from backend.lib.closure_standards import dangling_pattern_hit
from backend.lib.lang_profile import profile_for

BROKEN = (
    "你见过一块60吨的铁,八十万年前砸在地上,却没有砸出任何坑吗?"
    "1920年,纳米比亚一个农民犁地时,犁头突然卡住。后来发现,这就是地球上已知最大的完整陨石——霍巴铁陨石。"
    "按常理,一枚60吨的铁块以宇宙速度砸向地面,应该炸开几十米深的坑。"
    "但霍巴陨石周围,什么都没有。它就像被什么人轻轻放在草原上,只陷进去几十厘米。"
    "科学家用了近百年才找到解释。关键在三个条件同时凑齐:"
    "第一,霍巴陨石的形状像一块扁平的药片。这个形状在大气层里会产生巨大的阻力,像打水漂一样被空气托住。"
)


def test_catches_the_real_prod_incident():
    assert dangling_pattern_hit(BROKEN) == "dangling_enumeration"


def test_complete_enumeration_passes():
    ok = "原因有三个。第一,形状扁平。第二,速度被削。第三,土壤松软。所以它没砸出坑。"
    assert dangling_pattern_hit(ok) is None


def test_one_point_then_concluded_passes():
    # 开了「第一」但只讲一点、随后明确收尾 —— 不该误伤
    ok = "记住一点就够了。第一,坚持。所以说,贵在坚持,别的都是次要的。"
    assert dangling_pattern_hit(ok) is None


def test_shouhui_dangling():
    assert dangling_pattern_hit("遇到这种情况怎么办?首先,别慌。") == "dangling_enumeration"


def test_shouhui_resolved_passes():
    assert dangling_pattern_hit("遇到这种情况怎么办?首先,别慌。其次,马上报警。这样就稳了。") is None


def test_non_enumeration_first_not_flagged():
    # 「第一次/世界第一/第一名」不是枚举序数,不该命中
    assert dangling_pattern_hit("这是人类第一次拍到它。它是世界第一大的陨石,拿了第一名。") is None


def test_unanswered_howto_still_works():
    assert dangling_pattern_hit("蜂蜜水到底该怎么冲?水温多少度?") == "unanswered_howto"


def test_empty_and_normal():
    assert dangling_pattern_hit("") is None
    assert dangling_pattern_hit("这就是蜜獾无所畏惧的秘密,自然界远比我们想的精彩。") is None


# ── 真实闸路径回归 ──────────────────────────────────────────────────
# run_script_gate 走的是 profile.dangling_hit(**不是** dangling_pattern_hit 本身)。
# 早先枚举检测只加进 closure_standards,而 lang_profile 走一份重复的旧正则,


def test_zh_profile_catches_enumeration_live_path():
    # 这条是关键回归:等价于 run_script_gate 里 prof.dangling_hit(script)。
    assert profile_for("zh").dangling_hit(BROKEN) == "dangling_enumeration"


def test_zh_profile_delegates_to_single_source():
    prof = profile_for("zh")
    # 操作性提问与断头列表两类,profile 路径都应与单一事实源一致。
    assert prof.dangling_hit("蜂蜜水到底该怎么冲?水温多少度?") == "unanswered_howto"
    assert prof.dangling_hit("原因有三个。第一,形状扁平。第二,速度被削。所以没砸坑。") is None
    assert prof.dangling_hit("") is None


def test_en_profile_unaffected():
    # 英文仍走 _EN_DANGLING,中文枚举正则不该误伤英文稿。
    prof = profile_for("en")
    assert prof.dangling_hit("So how do you actually do it?") == "unanswered_howto"
    assert prof.dangling_hit("This is why the honey badger fears nothing.") is None
