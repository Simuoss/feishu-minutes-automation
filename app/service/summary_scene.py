"""纪要场景，以及按场景装配提示词。

讲课与会议两套纪要的结构完全不同，但配图相关的规则大部分是共用的。
共用部分放在中立的基础提示词里，场景差异单独成文件，装配时注入 SCENE_SECTION，
避免同一段规则在两个场景里各写一遍、改了一处忘了另一处。
"""

from pathlib import Path
from typing import Literal, cast

Scene = Literal["LECTURE", "MEETING"]

LECTURE: Scene = "LECTURE"
MEETING: Scene = "MEETING"
VALID_SCENES: tuple[Scene, ...] = (LECTURE, MEETING)

SCENE_LABELS: dict[Scene, str] = {LECTURE: "讲课", MEETING: "会议"}

PROMPTS_DIR = Path(__file__).parent / "prompts"
SCENE_SECTION_MARKER = "{{SCENE_SECTION}}"

SUMMARY_PROMPTS: dict[Scene, str] = {
    LECTURE: "lecture_summary.md",
    MEETING: "meeting_summary.md",
}


def normalize_scene(value: object) -> Scene | None:
    """把外部来的场景值收敛到合法取值，无法识别时返回 None。"""
    if not isinstance(value, str):
        return None
    upper = value.strip().upper()
    if upper in VALID_SCENES:
        return cast(Scene, upper)
    return None


def scene_label(scene: Scene) -> str:
    return SCENE_LABELS[scene]


def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    prompt = path.read_text(encoding="utf-8")
    if not prompt.strip():
        raise RuntimeError(f"提示词文件为空，无法使用: {path}")
    return prompt


def load_summary_prompt(scene: Scene) -> str:
    return load_prompt(SUMMARY_PROMPTS[scene])


def load_scene_prompt(base_name: str, scene: Scene) -> str:
    """把场景差异段注入中立的基础提示词。

    base_name 形如 `screenshot_plan`，对应 `screenshot_plan.md` 与
    `screenshot_plan.lecture.md` / `screenshot_plan.meeting.md`。
    """
    base = load_prompt(f"{base_name}.md")
    if SCENE_SECTION_MARKER not in base:
        raise RuntimeError(
            f"基础提示词 {base_name}.md 里找不到 {SCENE_SECTION_MARKER}，"
            "场景差异段无处注入"
        )
    section = load_prompt(f"{base_name}.{scene.lower()}.md")
    return base.replace(SCENE_SECTION_MARKER, section.strip())
