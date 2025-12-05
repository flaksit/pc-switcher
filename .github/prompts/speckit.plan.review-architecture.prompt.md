---
description: Review a speckit feature plan
---

We use speckit as workflow to implement features in this project. The feature we are currently working has a specific feature folder under specs/ with the same name as the current branch. In the feature folder, there is a spec.md file that contains the specification of the feature.

My architect just made a detailed plan for the feature. The plan consists of all the contents of the feature folder (including subfolders), except for spec.md and except for the subfolder "checklists".
Review the architecture and design from the helicopter view of an architect. Is the architecture simple and clear, but not too simple? Does it follow best practices and common patterns? Is the architecture sound and is it easy to modify or extend to accomodate future features or change requests? Check if the solution isn't over-engineered. Check if the @.specify/memory/constitution.md is honoured.

Don't change any existing files. Write your feedback to a new file "plan-review-N-architecture.md" in the feature folder so I can feed it to the architect for them to fix/improve the plan. Replace N in the filename with the next available number starting from 1 (because there could be multiple reviews).
Example: If there is already a file "plan-review-1.md", create a new file "plan-review-2-architecture.md", and so on. Number all feedback points in the file so it is easy to reference them.


## Overriding remarks, feedback, instructions, if any
The following instructions override any conflicting instructions above. If there are no overriding instructions, this section is empty.
