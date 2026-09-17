# -*- coding: utf-8 -*-
"""临时脚本：单独重出「黑发少女」参考图（Codex 出图）。

原因：第一轮 Codex 把「人物设定图」理解成了设定表版式，出了一整张带
三面图/表情集/制服分解的日文设定表——做设计稿很好用，但做出片参考图不行：
满图文字会渗进每一帧。这里重出一条干净的单人立绘。

用完可删（项目里 `_` 开头是手搓临时脚本的约定）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from service.assets import codex_image  # noqa: E402
from shared.paths import OUTPUT  # noqa: E402

DESC = (
    "日式校园动画人物立绘。一名17岁日本高一女学生：黑色及肩内扣短发，发梢别着一枚深蓝色小发夹；"
    "个子高挑，身材修长；眼神清亮带一点冷感，看人直接，嘴角微扬时左边脸颊有一个小梨涡。"
    "服装：深蓝色西装外套敞开不系扣，里面白色衬衫且下摆没有扎进裙子，深蓝色领结松松垮垮地系着，"
    "黑红格子百褶裙，白色及膝袜，黑色皮鞋。右手腕戴着一条深蓝色编绳手环。"
    "构图：单独一个人物，正面站立，全身可见，人物居中，浅蓝色纯色背景，"
    "MAPPA风格赛璐璐平涂，丝滑2D日系动漫质感，高质量。"
    "只画这一个单人立绘——不要设定表版式：不要多视图、不要三面图、不要表情集、不要服装分解图、"
    "不要色卡、不要分镜框、不要注释文字，画面里一个字都不能有，不要日文假名、不要字母、不要水印。"
)


def main() -> None:
    dest = OUTPUT / "_assets_rishi_剧情_校园恋爱" / "characters" / "黑发少女_02.png"
    print(f"目标：{dest}")
    r = codex_image(DESC, dest)
    print(r)


if __name__ == "__main__":
    main()
