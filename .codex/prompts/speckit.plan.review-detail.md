---
description: Review a speckit feature plan
---

We use speckit as workflow to implement features in this project. The feature we are currently working has a specific feature folder under specs/ with the same name as the current branch. In the feature folder, there is a spec.md file that contains the specification of the feature.

My architect just made a detailed plan for the feature. The plan consists of all the contents of the feature folder (including subfolders), except for spec.md and except for the subfolder "checklists".
Review their plan on completeness, correctness, consistency. Be thorough in verifying that the plan fully addresses all spec requirements. However, "thorough" means checking everything—not finding fault with everything. The goal is a plan that's ready to implement, not one that's been critiqued from every angle.

Don't change any existing files. Write your feedback to a new file "plan-review-N-detail.md" in the feature folder so I can feed it to the architect for them to fix/improve the plan. Replace N in the filename with the next available number starting from 1 (because there could be multiple reviews).
Example: If there is already a file "plan-review-1.md", create a new file "plan-review-2-detail.md", and so on. Number all feedback points in the file so it is easy to reference them.

**Materiality guidance:** Only raise issues where:
- The plan contradicts or omits a spec requirement
- Implementation details are incorrect or would fail (wrong commands, impossible flows)
- There are internal inconsistencies between plan documents

Do NOT raise issues about:
- Minor formatting or organization preferences
- Alternative equally-valid approaches
- Details that are correct but could be "even better"

**If the plan correctly and completely addresses the spec:** Write a brief review confirming completeness. Example:
> The plan fully covers all spec requirements. No gaps or contradictions found.


## Overriding remarks, feedback, instructions, if any
The following instructions override any conflicting instructions above. If there are no overriding instructions, this section is empty.

$ARGUMENTS
