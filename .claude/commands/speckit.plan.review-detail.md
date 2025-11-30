---
description: Review a speckit feature plan
---

## User Input

```text
$EXTRA_INSTRUCTIONS
```

You **MUST** consider the user input before proceeding (if not empty). The user input takes precedence over any conflicting instructions in this prompt.


We use speckit as workflow to implement features in this project. The feature we are currently working has a specific feature folder under specs/ with the same name as the current branch. In the feature folder, there is a spec.md file that contains the specification of the feature.

My architect just made a detailed plan for the feature. The plan consists of all the contents of the feature folder (including subfolders), except for spec.md and except for the subfolder "checklists".
Review their plan on completeness, correctness, consistency. Give any other valuable feedback you might have. Don't change any existing files. Write your feedback to a new file "plan-review-N-detail.md" in the feature folder so I can feed it to the architect for them to fix/improve the plan. Replace N in the filename with the next available number starting from 1 (because there could be multiple reviews).
Example: If there is already a file "plan-review-1.md", create a new file "plan-review-2-detail.md", and so on.


## Overriding remarks, feedback, instructions, if any
The following instructions override any conflicting instructions above. If there are no overriding instructions, this section is empty.

$EXTRA_INSTRUCTIONS
