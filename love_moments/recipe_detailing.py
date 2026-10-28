"""Deterministic Chinese cooking details for the built-in recipe library.

The original seed file intentionally stays compact and readable.  This module
adds measured seasonings, preparation notes and structured cooking cues before
the recipes are validated and written to SQLite.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .recipe_catalog_quality import inferred_allergens, safety_tip


SEASONING_NAMES = {
    "盐", "白糖", "冰糖", "红糖", "生抽", "老抽", "米醋", "香醋", "陈醋", "料酒",
    "植物油", "橄榄油", "香油", "淀粉", "花椒粉", "黑胡椒", "白胡椒粉", "郫县豆瓣酱",
    "番茄酱", "咖喱块", "蛋黄酱", "凯撒沙拉酱", "蜂蜜", "黄油", "肉桂粉", "可可粉",
    "蒸鱼豉油", "蚝油", "鸡精", "干辣椒", "姜", "大蒜", "香葱", "罗勒叶", "水",
}


def _items(*values: tuple[str, float, str] | tuple[str, float, str, bool]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in values:
        name, quantity, unit, *optional = value
        result.append({"name": name, "quantity": quantity, "unit": unit, "optional": bool(optional and optional[0])})
    return result


# Quantities are for the base serving count of each recipe.  They are cooking
# references rather than dietary/medical prescriptions and scale with servings.
RECIPE_SEASONINGS: dict[str, list[dict[str, Any]]] = {
    "recipe_builtin_tomato_egg": _items(("盐", 2, "克"), ("白糖", 3, "克"), ("植物油", 15, "毫升"), ("水", 15, "毫升")),
    "recipe_builtin_cola_wings": _items(("盐", 1, "克"), ("料酒", 10, "毫升"), ("生抽", 15, "毫升"), ("老抽", 5, "毫升"), ("植物油", 10, "毫升")),
    "recipe_builtin_braised_pork": _items(("盐", 2, "克"), ("料酒", 15, "毫升"), ("生抽", 20, "毫升"), ("老抽", 5, "毫升"), ("冰糖", 20, "克"), ("水", 600, "毫升")),
    "recipe_builtin_sweet_sour_ribs": _items(("盐", 2, "克"), ("料酒", 15, "毫升"), ("生抽", 20, "毫升"), ("米醋", 25, "毫升"), ("白糖", 25, "克"), ("水", 500, "毫升")),
    "recipe_builtin_yuxiang_pork": _items(("盐", 1, "克"), ("料酒", 8, "毫升"), ("生抽", 10, "毫升"), ("米醋", 10, "毫升"), ("白糖", 8, "克"), ("淀粉", 6, "克"), ("植物油", 20, "毫升")),
    "recipe_builtin_kungpao_chicken": _items(("盐", 1, "克"), ("料酒", 8, "毫升"), ("生抽", 12, "毫升"), ("米醋", 10, "毫升"), ("白糖", 8, "克"), ("淀粉", 6, "克"), ("植物油", 20, "毫升")),
    "recipe_builtin_pepper_pork": _items(("盐", 1.5, "克"), ("料酒", 8, "毫升"), ("生抽", 12, "毫升"), ("淀粉", 6, "克"), ("植物油", 18, "毫升")),
    "recipe_builtin_mapo_tofu": _items(("盐", 1, "克"), ("生抽", 10, "毫升"), ("郫县豆瓣酱", 20, "克"), ("花椒粉", 1, "克"), ("淀粉", 5, "克"), ("植物油", 18, "毫升"), ("水", 180, "毫升")),
    "recipe_builtin_di_san_xian": _items(("盐", 2, "克"), ("生抽", 12, "毫升"), ("白糖", 2, "克"), ("淀粉", 4, "克"), ("植物油", 30, "毫升")),
    "recipe_builtin_garlic_broccoli": _items(("盐", 2, "克"), ("植物油", 12, "毫升"), ("水", 30, "毫升")),
    "recipe_builtin_dry_pot_cauliflower": _items(("盐", 1, "克"), ("生抽", 12, "毫升"), ("白糖", 2, "克"), ("植物油", 15, "毫升")),
    "recipe_builtin_sour_spicy_potato": _items(("盐", 2, "克"), ("米醋", 15, "毫升"), ("白糖", 1, "克"), ("植物油", 15, "毫升")),
    "recipe_builtin_steamed_seabass": _items(("盐", 2, "克"), ("料酒", 10, "毫升"), ("蒸鱼豉油", 20, "毫升"), ("植物油", 12, "毫升")),
    "recipe_builtin_braised_fish": _items(("盐", 2, "克"), ("料酒", 15, "毫升"), ("生抽", 20, "毫升"), ("老抽", 4, "毫升"), ("白糖", 3, "克"), ("植物油", 20, "毫升"), ("水", 350, "毫升")),
    "recipe_builtin_shrimp_egg": _items(("盐", 2, "克"), ("料酒", 5, "毫升"), ("淀粉", 3, "克"), ("植物油", 15, "毫升"), ("水", 15, "毫升")),
    "recipe_builtin_seaweed_egg_soup": _items(("盐", 2, "克"), ("白胡椒粉", 0.5, "克"), ("香油", 3, "毫升"), ("水", 700, "毫升")),
    "recipe_builtin_wintermelon_rib_soup": _items(("盐", 3, "克"), ("料酒", 10, "毫升"), ("水", 1200, "毫升"), ("白胡椒粉", 0.5, "克", True)),
    "recipe_builtin_corn_rib_soup": _items(("盐", 3, "克"), ("料酒", 10, "毫升"), ("水", 1300, "毫升"), ("白胡椒粉", 0.5, "克", True)),
    "recipe_builtin_yangzhou_fried_rice": _items(("盐", 2, "克"), ("生抽", 5, "毫升"), ("植物油", 18, "毫升"), ("白胡椒粉", 0.5, "克", True)),
    "recipe_builtin_home_fried_noodles": _items(("盐", 1, "克"), ("生抽", 15, "毫升"), ("老抽", 3, "毫升"), ("料酒", 5, "毫升"), ("植物油", 18, "毫升")),
    "recipe_builtin_pork_cabbage_dumplings": _items(("盐", 5, "克"), ("生抽", 20, "毫升"), ("料酒", 10, "毫升"), ("香油", 8, "毫升"), ("水", 40, "毫升")),
    "recipe_builtin_pork_wonton": _items(("盐", 4, "克"), ("生抽", 15, "毫升"), ("料酒", 8, "毫升"), ("香油", 6, "毫升"), ("白胡椒粉", 1, "克"), ("水", 1000, "毫升")),
    "recipe_builtin_century_egg_congee": _items(("盐", 2.5, "克"), ("料酒", 5, "毫升"), ("淀粉", 3, "克"), ("香油", 3, "毫升"), ("水", 1200, "毫升")),
    "recipe_builtin_scallion_oil_noodles": _items(("生抽", 20, "毫升"), ("老抽", 5, "毫升"), ("白糖", 6, "克"), ("植物油", 35, "毫升")),
    "recipe_builtin_pumpkin_millet_congee": _items(("水", 900, "毫升"), ("冰糖", 10, "克", True)),
    "recipe_builtin_chive_egg_pancake": _items(("盐", 2, "克"), ("水", 180, "毫升"), ("植物油", 12, "毫升"), ("白胡椒粉", 0.5, "克", True)),
    "recipe_builtin_minced_pork_eggplant": _items(("盐", 1, "克"), ("生抽", 15, "毫升"), ("料酒", 8, "毫升"), ("白糖", 3, "克"), ("淀粉", 4, "克"), ("植物油", 20, "毫升"), ("水", 100, "毫升")),
    "recipe_builtin_twice_cooked_pork": _items(("盐", 1, "克"), ("料酒", 10, "毫升"), ("生抽", 10, "毫升"), ("郫县豆瓣酱", 20, "克"), ("白糖", 2, "克"), ("植物油", 10, "毫升")),
    "recipe_builtin_mushu_pork": _items(("盐", 2, "克"), ("料酒", 8, "毫升"), ("生抽", 10, "毫升"), ("淀粉", 5, "克"), ("植物油", 18, "毫升")),
    "recipe_builtin_stir_fried_greens": _items(("盐", 2, "克"), ("植物油", 12, "毫升"), ("水", 15, "毫升")),
    "recipe_builtin_blanch_choy_sum": _items(("盐", 1, "克"), ("生抽", 15, "毫升"), ("植物油", 12, "毫升"), ("水", 1000, "毫升")),
    "recipe_builtin_lotus_rib_soup": _items(("盐", 3, "克"), ("料酒", 10, "毫升"), ("水", 1300, "毫升"), ("白胡椒粉", 0.5, "克", True)),
    "recipe_builtin_mushroom_greens": _items(("盐", 1.5, "克"), ("生抽", 10, "毫升"), ("蚝油", 8, "毫升"), ("植物油", 12, "毫升"), ("水", 30, "毫升")),
    "recipe_builtin_pickled_fish": _items(("盐", 3, "克"), ("料酒", 12, "毫升"), ("淀粉", 8, "克"), ("白胡椒粉", 1, "克"), ("植物油", 25, "毫升"), ("水", 1000, "毫升")),
    "recipe_builtin_spicy_beef": _items(("盐", 2, "克"), ("料酒", 10, "毫升"), ("生抽", 12, "毫升"), ("淀粉", 8, "克"), ("植物油", 30, "毫升"), ("水", 800, "毫升")),
    "recipe_builtin_ginger_steamed_chicken": _items(("盐", 3, "克"), ("料酒", 15, "毫升"), ("生抽", 10, "毫升"), ("香油", 5, "毫升")),
    "recipe_builtin_egg_sandwich": _items(("盐", 1, "克"), ("黑胡椒", 0.5, "克"), ("蛋黄酱", 20, "克")),
    "recipe_builtin_fried_egg_toast": _items(("盐", 0.5, "克"), ("黑胡椒", 0.5, "克"), ("黄油", 8, "克")),
    "recipe_builtin_bolognese_pasta": _items(("盐", 3, "克"), ("黑胡椒", 1, "克"), ("橄榄油", 15, "毫升"), ("番茄酱", 30, "克"), ("水", 1000, "毫升")),
    "recipe_builtin_cream_mushroom_pasta": _items(("盐", 2.5, "克"), ("黑胡椒", 1, "克"), ("橄榄油", 10, "毫升"), ("水", 1000, "毫升")),
    "recipe_builtin_pan_seared_steak": _items(("盐", 3, "克"), ("黑胡椒", 1.5, "克"), ("植物油", 10, "毫升"), ("黄油", 20, "克")),
    "recipe_builtin_black_pepper_chicken": _items(("盐", 1, "克"), ("黑胡椒", 2, "克"), ("生抽", 12, "毫升"), ("蜂蜜", 10, "克"), ("植物油", 10, "毫升")),
    "recipe_builtin_caesar_chicken_salad": _items(("盐", 1, "克"), ("黑胡椒", 0.5, "克"), ("橄榄油", 10, "毫升"), ("凯撒沙拉酱", 35, "克")),
    "recipe_builtin_mashed_potato": _items(("盐", 2, "克"), ("黑胡椒", 0.5, "克", True), ("黄油", 20, "克")),
    "recipe_builtin_pumpkin_soup": _items(("盐", 2, "克"), ("黑胡椒", 0.5, "克", True), ("黄油", 12, "克"), ("水", 250, "毫升")),
    "recipe_builtin_cream_mushroom_soup": _items(("盐", 2, "克"), ("黑胡椒", 0.5, "克"), ("黄油", 15, "克"), ("水", 200, "毫升")),
    "recipe_builtin_seafood_baked_rice": _items(("盐", 2, "克"), ("黑胡椒", 0.5, "克"), ("橄榄油", 10, "毫升")),
    "recipe_builtin_curry_chicken_rice": _items(("盐", 1, "克"), ("植物油", 15, "毫升"), ("水", 650, "毫升")),
    "recipe_builtin_herb_roast_chicken": _items(("盐", 8, "克"), ("黑胡椒", 2, "克"), ("橄榄油", 15, "毫升"), ("黄油", 35, "克")),
    "recipe_builtin_garlic_roast_shrimp": _items(("盐", 2, "克"), ("黑胡椒", 0.5, "克"), ("黄油", 20, "克"), ("橄榄油", 8, "毫升")),
    "recipe_builtin_tuna_sandwich": _items(("盐", 1, "克"), ("黑胡椒", 0.5, "克"), ("蛋黄酱", 25, "克")),
    "recipe_builtin_french_toast": _items(("白糖", 12, "克"), ("盐", 0.5, "克"), ("黄油", 15, "克")),
    "recipe_builtin_avocado_egg_toast": _items(("盐", 1, "克"), ("黑胡椒", 0.5, "克"), ("橄榄油", 5, "毫升", True)),
    "recipe_builtin_bacon_mushroom_frittata": _items(("盐", 1, "克"), ("黑胡椒", 0.5, "克"), ("橄榄油", 8, "毫升")),
    "recipe_builtin_margherita_pizza": _items(("盐", 1, "克"), ("橄榄油", 8, "毫升"), ("番茄酱", 80, "克")),
    "recipe_builtin_cheeseburger": _items(("盐", 2, "克"), ("黑胡椒", 1, "克"), ("植物油", 8, "毫升")),
    "recipe_builtin_roasted_vegetables": _items(("盐", 3, "克"), ("黑胡椒", 1, "克"), ("橄榄油", 25, "毫升")),
    "recipe_builtin_lemon_butter_salmon": _items(("盐", 2.5, "克"), ("黑胡椒", 1, "克"), ("植物油", 8, "毫升"), ("黄油", 20, "克")),
    "recipe_builtin_tiramisu_cup": _items(("白糖", 35, "克"), ("可可粉", 10, "克")),
    "recipe_builtin_apple_cinnamon_oats": _items(("白糖", 8, "克", True), ("肉桂粉", 1, "克"), ("盐", 0.5, "克")),
    "recipe_builtin_honey_garlic_pork": _items(("盐", 2, "克"), ("黑胡椒", 1, "克"), ("蜂蜜", 20, "克"), ("黄油", 15, "克"), ("植物油", 8, "毫升")),
    "recipe_builtin_chicken_vegetable_salad": _items(("盐", 1.5, "克"), ("黑胡椒", 0.5, "克"), ("橄榄油", 15, "毫升"), ("米醋", 8, "毫升")),
    "recipe_builtin_beef_potato_stew": _items(("盐", 4, "克"), ("黑胡椒", 1, "克"), ("橄榄油", 15, "毫升"), ("番茄酱", 25, "克"), ("水", 1000, "毫升")),
    "recipe_builtin_garlic_bread": _items(("盐", 1, "克"), ("黄油", 30, "克"), ("黑胡椒", 0.5, "克", True)),
    "recipe_builtin_yogurt_fruit_bowl": _items(("蜂蜜", 15, "克", True)),
}


PREPARATION_EXACT = {
    "鸡中翅": "厨房纸吸干，两面各划两刀便于入味",
    "五花肉": "切成约 3 厘米方块，冷水下锅焯去浮沫",
    "猪肋排": "剁成 4 厘米段，冷水浸泡 15 分钟后焯水",
    "猪里脊": "去筋膜并逆纹切丝，粗细尽量一致",
    "牛里脊": "逆纹切约 2 毫米薄片，拌腌料后静置",
    "鸡胸肉": "去筋膜后切丁或拍至厚薄均匀，厨房纸吸干",
    "鸡腿肉": "去多余脂肪并擦干，厚处划开使受热均匀",
    "鸡腿": "擦干表面，在关节和厚肉处划开便于熟透",
    "整鸡": "去内脏并擦干，室温回温 20 分钟；生禽不要冲洗",
    "牛排": "厨房纸吸干，烹饪前回温 20 分钟",
    "牛腩": "切约 3 厘米块，冷水下锅焯水后洗净浮沫",
    "猪肉末": "提前解冻，拌散备用",
    "牛肉末": "提前解冻，拌散备用",
    "猪肉馅": "顺同一方向搅拌至有黏性",
    "鲈鱼": "去鳞、鳃和内脏，擦干后在鱼身浅划两刀",
    "鲫鱼": "处理干净并彻底擦干，鱼身浅划两刀",
    "黑鱼片": "吸干水分，按片分散后腌制",
    "三文鱼": "检查鱼刺并擦干，鱼皮表面保持干燥",
    "虾仁": "去虾线、吸干水分后轻抓腌制",
    "鲜虾": "剪须、开背去虾线并擦干",
    "鱿鱼": "去膜和软骨，切花刀后沥干",
    "番茄": "洗净去蒂，切成约 2 厘米块",
    "鸡蛋": "磕入干净碗中，充分打散至蛋清蛋黄融合",
    "土豆": "去皮切好后用清水冲去表面淀粉并沥干",
    "茄子": "切均匀条块；下锅前再处理以减少氧化",
    "青椒": "去蒂去籽，切成大小一致的丝或块",
    "西兰花": "切小朵，淡盐水浸泡 5 分钟后冲洗沥干",
    "花菜": "掰成小朵，清洗后彻底沥干",
    "冬瓜": "去皮去瓤，切约 2 厘米厚块",
    "甜玉米": "洗净后切成 3 厘米段",
    "胡萝卜": "去皮后切丁、片或滚刀块，保持大小一致",
    "白菜": "切碎后撒少量配方盐静置 10 分钟，攥去多余水分",
    "韭菜": "洗净彻底沥干，再切成约 5 毫米末",
    "莲藕": "去皮切厚片，用清水冲洗孔洞中的泥沙",
    "青菜": "逐片洗净并充分沥干，菜梗和菜叶可分开放",
    "菜心": "摘去老叶，洗净后沥干",
    "鲜香菇": "擦洗干净，去硬蒂后切片",
    "口蘑": "擦洗后切约 4 毫米厚片",
    "酸菜": "挤去多余汁水，过咸时快速冲洗后切段",
    "南瓜": "去皮去瓤，切成约 2 厘米块",
    "洋葱": "去皮后切细丁或薄丝",
    "生菜": "逐片洗净，甩干或用厨房纸吸干",
    "吐司": "按成品需要去边或切片，保持表面干燥",
    "厚吐司": "切约 2 厘米厚片，保持表面干燥",
    "意大利面": "称量备用；另备足量沸水煮面",
    "鲜面条": "抖散并准备足量沸水，避免入锅粘连",
    "米饭": "使用完全放凉的米饭，提前拨散",
    "大米": "淘洗 2 次后沥干，可浸泡 20 分钟",
    "小米": "快速淘洗 1 次，避免反复搓洗",
    "面粉": "过筛后再加液体，减少结块",
    "牛油果": "对半切开去核，取果肉后立即加柠檬汁",
    "草莓": "去蒂前冲洗，沥干后再切块",
    "苹果": "洗净去核，切约 1 厘米丁",
}


def _preparation(name: str, group: str) -> str:
    if name in PREPARATION_EXACT:
        return PREPARATION_EXACT[name]
    if group == "seasoning":
        return "按配方量提前称好，分开放置"
    if any(word in name for word in ("肉", "排", "鸡", "鱼", "虾", "牛", "猪")):
        return "厨房纸吸干并按食谱切成大小一致的块"
    if any(word in name for word in ("菜", "瓜", "椒", "笋", "菇", "豆", "葱")):
        return "洗净、沥干并按食谱切配"
    return "按配方量称好备用"


def _step_title(instruction: str, position: int) -> str:
    if any(word in instruction for word in ("腌", "浸", "打匀", "拌匀", "切", "擦干", "回温")):
        return "处理与调味"
    if any(word in instruction for word in ("焯", "煮开", "烧开")):
        return "焯煮或烧开"
    if "蒸" in instruction:
        return "上锅蒸制"
    if "烤" in instruction:
        return "烤制上色"
    if "煎" in instruction:
        return "煎至成熟"
    if any(word in instruction for word in ("炒", "爆香", "翻匀")):
        return "下锅翻炒"
    if any(word in instruction for word in ("炖", "焖", "熬")):
        return "炖煮入味"
    if any(word in instruction for word in ("冷藏", "装盘", "盛出", "挤", "撒", "铺")):
        return "组合与收尾"
    return f"烹饪第 {position} 步"


def _heat_level(instruction: str) -> str:
    if any(word in instruction for word in ("冷藏", "拌匀", "打匀", "切", "静置", "装盘", "铺", "压")) and not any(word in instruction for word in ("炒", "煮", "煎", "烤", "蒸")):
        return "无需加热"
    if "烤" in instruction:
        return "按步骤温度预热烤箱"
    if "蒸" in instruction:
        return "水开后中大火"
    if any(word in instruction for word in ("炖", "焖", "熬", "小火")):
        return "烧开后小火"
    if any(word in instruction for word in ("爆香", "大火", "快速", "翻炒")):
        return "中大火"
    if any(word in instruction for word in ("煎", "炒", "煮")):
        return "中火"
    return "按步骤调整"


def _timer_seconds(instruction: str, cook_minutes: int, step_count: int) -> int | None:
    if "冷藏" in instruction:
        return 4 * 60 * 60
    if any(word in instruction for word in ("切", "擦干", "打匀", "拌匀", "铺", "装盘")) and not any(word in instruction for word in ("炒", "煮", "煎", "烤", "蒸")):
        return None
    if "焯" in instruction:
        return 120
    if "烤" in instruction and cook_minutes:
        return cook_minutes * 60
    if "蒸" in instruction and cook_minutes:
        return max(5 * 60, cook_minutes * 60)
    if any(word in instruction for word in ("炖", "焖", "熬")) and cook_minutes:
        return max(10 * 60, (cook_minutes - 8) * 60)
    if any(word in instruction for word in ("炒", "爆香")):
        return min(8 * 60, max(2 * 60, cook_minutes * 60 // max(step_count, 1)))
    if any(word in instruction for word in ("煎", "煮")):
        return max(3 * 60, cook_minutes * 60 // max(step_count, 1)) if cook_minutes else None
    return None


def _key_point(instruction: str) -> str:
    if "猪" in instruction or any(word in instruction for word in ("鸡肉", "鸡胸", "鸡腿", "鸡翅", "鸡块", "整鸡")):
        return "最厚处完全变色、无粉红生肉和血水后再进入下一步。"
    if "牛排" in instruction:
        return "按喜欢的熟度掌握时间，出锅后静置再切。"
    if any(word in instruction for word in ("鱼", "虾", "海鲜", "鱿鱼")):
        return "肉质刚变为不透明且有弹性即可，避免久煮变老。"
    if any(word in instruction for word in ("炒", "爆香")):
        return "锅内保持明显滋啦声，食材铺开而不是堆在一起。"
    if any(word in instruction for word in ("炖", "焖", "熬")):
        return "保持轻微冒泡；水量不足时只补热水。"
    if "烤" in instruction:
        return "烤箱先充分预热，以表面上色且中心熟透为准。"
    if "蒸" in instruction:
        return "从水重新沸腾后开始计时，途中尽量不要频繁开盖。"
    if any(word in instruction for word in ("蛋", "打匀")):
        return "蛋液均匀、没有明显蛋清结块。"
    return "观察颜色、香气和质地；达到步骤描述的状态再继续。"


def _quantity_label(item: dict[str, Any]) -> str:
    quantity = item.get("quantity")
    quantity_text = str(item.get("quantity_text", "")).strip()
    if quantity is None:
        amount = quantity_text or "适量"
    else:
        amount = f"{quantity:g}" if isinstance(quantity, float) else str(quantity)
        amount = f"{amount} {item.get('unit', '')}".strip()
    return f"{item.get('name', '')} {amount}{'（可选）' if item.get('optional') else ''}"


BAKING_GUIDANCE = {
    "recipe_builtin_seafood_baked_rice": "烤箱提前预热至 200℃，放中层烤 12–15 分钟，芝士完全融化并出现浅金色斑点即可。",
    "recipe_builtin_herb_roast_chicken": "烤箱提前预热至 200℃，放中下层烤约 55–65 分钟；最厚处完全熟透后取出静置 10 分钟。",
    "recipe_builtin_garlic_roast_shrimp": "烤箱提前预热至 200℃，虾单层排开烤 8–10 分钟，刚卷曲变色即可。",
    "recipe_builtin_bacon_mushroom_frittata": "烤箱提前预热至 180℃，放中层烤 10–12 分钟，中心刚凝固即可。",
    "recipe_builtin_margherita_pizza": "烤箱连同烤盘提前预热至 230℃，放中层烤 10–15 分钟，边缘鼓起上色且芝士冒泡即可。",
    "recipe_builtin_roasted_vegetables": "烤箱提前预热至 210℃，蔬菜单层铺开烤 25–30 分钟，中途翻面一次。",
    "recipe_builtin_garlic_bread": "烤箱提前预热至 190℃，放中层烤 8–10 分钟，边缘金黄酥脆即可。",
}


ORIGINAL_PASSIVE_MINUTES = {
    "recipe_builtin_tiramisu_cup": 240,
}


def enrich_builtin_recipe(raw_recipe: dict[str, Any]) -> dict[str, Any]:
    """Return a v2 built-in recipe with measurable and displayable detail."""

    recipe = deepcopy(raw_recipe)
    if (
        int(recipe.get("version", 1)) >= 2
        and recipe.get("ingredients")
        and all(item.get("group") and item.get("preparation") for item in recipe["ingredients"])
        and recipe.get("steps")
        and all(
            isinstance(step, dict)
            and step.get("title")
            and step.get("instruction")
            and step.get("heat_level")
            and step.get("key_point")
            for step in recipe["steps"]
        )
    ):
        return recipe
    recipe_id = str(recipe.get("id", ""))
    ingredient_by_name: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for original in recipe.get("ingredients", []):
        item = dict(original)
        name = str(item.get("name", ""))
        group = "seasoning" if name in SEASONING_NAMES else "main"
        item["group"] = group
        item["preparation"] = item.get("preparation") or _preparation(name, group)
        ingredient_by_name[name] = item
        ordered.append(item)
    for seasoning in RECIPE_SEASONINGS.get(recipe_id, []):
        item = ingredient_by_name.get(seasoning["name"])
        if item is None:
            item = dict(seasoning)
            ordered.append(item)
            ingredient_by_name[seasoning["name"]] = item
        else:
            item.update({key: value for key, value in seasoning.items() if key != "name"})
        item["group"] = "seasoning"
        item["preparation"] = "按配方量提前称好，分开放置"
    recipe["ingredients"] = ordered

    prep_notes = [f"{item['name']}：{item['preparation']}" for item in ordered if item.get("group") == "main"][:4]
    seasoning_notes = "、".join(_quantity_label(item) for item in ordered if item.get("group") == "seasoning")
    detailed_steps: list[dict[str, Any]] = [{
        "title": "备料与分区",
        "instruction": "；".join(prep_notes) + f"。调味料分装：{seasoning_notes}。生熟食材使用不同砧板和刀具。",
        "timer_seconds": max(60, int(recipe.get("prep_minutes", 5)) * 60),
        "heat_level": "无需加热",
        "key_point": "全部切配和称量完成后再开火，避免中途寻找调料导致食材过火。",
    }]
    original_steps = recipe.get("steps", [])
    for index, original in enumerate(original_steps, 1):
        value = {"instruction": original} if isinstance(original, str) else dict(original)
        instruction = str(value.get("instruction", "")).strip()
        if recipe_id in BAKING_GUIDANCE and "烤" in instruction:
            instruction = BAKING_GUIDANCE[recipe_id]
        timer_seconds = value.get("timer_seconds") or _timer_seconds(
            instruction,
            int(recipe.get("cook_minutes", 0)),
            len(original_steps),
        )
        if not timer_seconds:
            timer_seconds = max(
                60,
                int(recipe.get("cook_minutes", 0) or recipe.get("prep_minutes", 0) or 5)
                * 60
                // max(len(original_steps), 1),
            )
        heat_level = str(value.get("heat_level") or "").strip()
        if not heat_level or heat_level == "按步骤调整":
            heat_level = _heat_level(instruction)
        if heat_level == "按步骤调整":
            heat_level = "无需加热"
        detailed_steps.append({
            "title": value.get("title") or _step_title(instruction, index),
            "instruction": instruction,
            "timer_seconds": timer_seconds,
            "heat_level": heat_level,
            "key_point": value.get("key_point") or _key_point(instruction),
        })
    recipe["steps"] = detailed_steps
    recipe["passive_minutes"] = ORIGINAL_PASSIVE_MINUTES.get(recipe_id, 0)
    ingredient_names = {str(item.get("name", "")) for item in ordered}
    # Rebuild from the actual ingredient table.  Legacy hand-authored labels
    # are deliberately discarded because a few old rows claimed soy even when
    # no soybean ingredient or soy sauce was listed.
    recipe["allergen_tags"] = inferred_allergens(ingredient_names)
    tips = list(recipe.get("tips", []))
    storage_tip = "易腐食物在室温放置不要超过2小时；剩余食物应及时分装冷藏。"
    if storage_tip not in tips:
        tips.append(storage_tip)
    safe = safety_tip(ingredient_names)
    if safe and safe not in tips:
        tips.append(safe)
    recipe["tips"] = tips
    recipe["version"] = max(4, int(recipe.get("version", 1)))
    return recipe
