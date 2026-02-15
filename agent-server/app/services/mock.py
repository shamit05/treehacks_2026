# app/services/mock.py
# Owner: Eng 3 (Agent Pipeline)
#
# Returns hardcoded valid StepPlans for demo mode.
# Activated when MOCK_MODE=true environment variable is set.

from app.schemas.step_plan import (
    Advance,
    AdvanceType,
    ImageSize,
    Step,
    StepPlan,
    TargetRect,
)


def get_mock_next_step(
    goal: str,
    image_size: ImageSize | None = None,
    next_step_number: int = 2,
) -> StepPlan:
    """Return a single mock next-step that validates against the schema."""
    size = image_size or ImageSize(w=1920, h=1080)

    return StepPlan(
        version="v1",
        goal=goal,
        image_size=size,
        steps=[
            Step(
                id=f"s{next_step_number}",
                instruction=f"Click the next button to continue (step {next_step_number}).",
                targets=[
                    TargetRect(
                        x=0.45,
                        y=0.5,
                        w=0.1,
                        h=0.04,
                        confidence=0.88,
                        label="Next button",
                    )
                ],
                advance=Advance(type=AdvanceType.click_in_target),
            ),
        ],
    )


def get_mock_plan(goal: str, image_size: ImageSize | None = None) -> StepPlan:
    """Return a mock step plan that validates against the schema."""
    size = image_size or ImageSize(w=1920, h=1080)

    return StepPlan(
        version="v1",
        goal=goal,
        image_size=size,
        steps=[
            Step(
                id="s1",
                instruction="Click the menu bar item to begin.",
                targets=[
                    TargetRect(
                        x=0.02,
                        y=0.0,
                        w=0.06,
                        h=0.03,
                        confidence=0.9,
                        label="Menu bar",
                    )
                ],
                advance=Advance(type=AdvanceType.click_in_target),
            ),
            Step(
                id="s2",
                instruction="Select 'New...' from the dropdown menu.",
                targets=[
                    TargetRect(
                        x=0.02,
                        y=0.04,
                        w=0.12,
                        h=0.03,
                        confidence=0.85,
                        label="New menu item",
                    )
                ],
                advance=Advance(type=AdvanceType.click_in_target),
            ),
            Step(
                id="s3",
                instruction="Type your information in the text field.",
                targets=[
                    TargetRect(
                        x=0.25,
                        y=0.3,
                        w=0.5,
                        h=0.05,
                        confidence=0.8,
                        label="Text input field",
                    )
                ],
                advance=Advance(type=AdvanceType.text_entered_or_next),
            ),
            Step(
                id="s4",
                instruction="Click 'Save' to confirm.",
                targets=[
                    TargetRect(
                        x=0.7,
                        y=0.85,
                        w=0.1,
                        h=0.04,
                        confidence=0.9,
                        label="Save button",
                    )
                ],
                advance=Advance(type=AdvanceType.click_in_target),
            ),
        ],
    )
