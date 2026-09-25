from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class JiraConfig(BaseModel):
    base_url: str
    email: str
    api_token: str
    project_key: str


class AzureConfig(BaseModel):
    org: str
    project: str
    team: str
    pat: str


class StatusRule(BaseModel):
    match: List[str]
    no_assignee: Optional[bool] = None
    target_states: List[str]


class PriorityRule(BaseModel):
    jira_priority: str
    numeric: int
    custom_label: str


class MappingConfig(BaseModel):
    story_status_rules: List[StatusRule]
    task_status_rules: List[StatusRule]
    priority_rules: List[PriorityRule]
    assignee_overrides: Dict[str, str] = Field(default_factory=dict)


class DateFilter(BaseModel):
    field: str = "updated"          # "created" | "updated" | "either"
    from_date: Optional[str] = None # ISO date "YYYY-MM-DD"
    to_date: Optional[str] = None   # ISO date "YYYY-MM-DD"


class SyncRequest(BaseModel):
    jira: JiraConfig
    azure: AzureConfig
    mapping: MappingConfig
    date_filter: Optional[DateFilter] = None


# ── Status rules derived from the real boards ──────────────────────────────
#
# Example Jira workflow:
#   TO DO → IN PROGRESS → DEVOPS → QA REQUESTED → QA IN PROGRESS
#   → PENDING DEV → UAT → READY FOR LIVE → DONE
#
# Example Azure DevOps states:
#   Backlog → To do → Doing → In Review → Blocked → Done
#
# Mapping logic:
#   TO DO (no assignee) → Backlog   (unassigned, waiting to be picked up)
#   TO DO (assigned)    → To do     (queued for this person)
#   IN PROGRESS         → Doing
#   DEVOPS / QA *       → In Review (QA rules checked BEFORE "in progress"
#                                    because "QA In Progress" contains "in progress")
#   PENDING DEV         → Blocked   (waiting on clarification / re-work)
#   UAT                 → In Review (user acceptance testing — still being validated)
#   READY FOR LIVE      → Done      (validation complete, awaiting deployment)
#   DONE / LIVE etc.    → Done
# ───────────────────────────────────────────────────────────────────────────

DEFAULT_STORY_RULES: List[StatusRule] = [
    # TO DO — unassigned goes to Backlog; assigned goes to To do
    StatusRule(match=["to do"], no_assignee=True,  target_states=["Backlog"]),
    StatusRule(match=["to do"], no_assignee=False, target_states=["To do", "To Do", "New"]),
    # DEVOPS / QA stages — checked BEFORE "in progress" to prevent
    # "QA In Progress" from matching the generic in-progress rule below
    StatusRule(match=["devops", "qa requested", "qa in progress", "qa"],
               target_states=["In Review", "Review"]),
    # IN PROGRESS
    StatusRule(match=["in progress"],
               target_states=["Doing", "Active", "In Progress"]),
    # PENDING DEV — blocked / waiting on clarifications
    StatusRule(match=["pending dev"],
               target_states=["Blocked", "On Hold"]),
    # UAT — user acceptance testing, still being validated → In Review
    StatusRule(match=["uat"],
               target_states=["In Review", "Review", "Done"]),
    # READY FOR LIVE — development complete, queued for deployment → Done
    StatusRule(match=["ready for live", "ready"],
               target_states=["Done", "Closed", "Completed"]),
    # DONE / LIVE and other terminal states
    StatusRule(match=["done", "live", "closed", "resolved", "finished", "completed"],
               target_states=["Done", "Closed", "Completed"]),
]

DEFAULT_TASK_RULES: List[StatusRule] = [
    StatusRule(match=["to do"], no_assignee=True,  target_states=["Backlog"]),
    StatusRule(match=["to do"], no_assignee=False, target_states=["To do", "To Do", "New"]),
    StatusRule(match=["devops", "qa requested", "qa in progress", "qa"],
               target_states=["In Review", "Review"]),
    StatusRule(match=["in progress"],
               target_states=["Doing", "Active", "In Progress"]),
    StatusRule(match=["pending dev"],
               target_states=["Blocked", "On Hold"]),
    StatusRule(match=["uat"],
               target_states=["In Review", "Review", "Done"]),
    StatusRule(match=["ready for live", "ready"],
               target_states=["Done", "Closed", "Completed"]),
    StatusRule(match=["done", "live", "closed", "resolved", "finished", "completed"],
               target_states=["Done", "Closed", "Completed"]),
]

DEFAULT_PRIORITY_RULES: List[PriorityRule] = [
    PriorityRule(jira_priority="highest", numeric=1, custom_label="1 - Very High"),
    PriorityRule(jira_priority="high",    numeric=2, custom_label="2 - High"),
    PriorityRule(jira_priority="medium",  numeric=3, custom_label="3 - Medium"),
    PriorityRule(jira_priority="low",     numeric=4, custom_label="4 - Low"),
    PriorityRule(jira_priority="lowest",  numeric=4, custom_label="4 - Low"),
]


def default_mapping_config() -> MappingConfig:
    return MappingConfig(
        story_status_rules=[r.model_copy() for r in DEFAULT_STORY_RULES],
        task_status_rules=[r.model_copy() for r in DEFAULT_TASK_RULES],
        priority_rules=[r.model_copy() for r in DEFAULT_PRIORITY_RULES],
        assignee_overrides={},
    )


def resolve_state_generic(jira_status: str, available_states: list,
                           rules: List[StatusRule], assignee_email: str = None) -> str:
    normalised = (jira_status or "").strip().lower()
    has_assignee = bool(assignee_email and assignee_email.strip())
    lower_to_actual = {s.lower(): s for s in available_states}

    for rule in rules:
        if not any(kw.lower() in normalised for kw in rule.match):
            continue
        if rule.no_assignee is not None and rule.no_assignee != (not has_assignee):
            continue
        for candidate in rule.target_states:
            actual = lower_to_actual.get(candidate.lower())
            if actual:
                return actual
        break

    for fallback_hint in ["to do", "new", "proposed", "backlog", "open", "active"]:
        actual = lower_to_actual.get(fallback_hint)
        if actual:
            return actual
    return available_states[0] if available_states else "To Do"


def resolve_priority_generic(jira_priority: str, rules: List[PriorityRule]) -> tuple:
    normalised = (jira_priority or "").strip().lower()
    for rule in rules:
        if rule.jira_priority.strip().lower() == normalised:
            return rule.numeric, rule.custom_label
    for rule in rules:
        if rule.jira_priority.strip().lower() == "medium":
            return rule.numeric, rule.custom_label
    return 3, "3 - Medium"


def resolve_assignee_email_generic(jira_email: str, overrides: Dict[str, str]) -> str:
    if not jira_email:
        return jira_email
    return overrides.get(jira_email, jira_email)
