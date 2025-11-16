---
description: Review a speckit feature implementation
---

## User Input

```text
$EXTRA_INSTRUCTIONS
```

You **MUST** consider the user input before proceeding (if not empty). The user input takes precedence over any conflicting instructions in this prompt.


We use speckit as workflow to implement features in this project. The feature we are currently working has a specific feature folder under specs/ with the same name as the current branch. In the feature folder, there is a spec.md file that contains the specification of the feature.

My developer just implemented the feature.
Review the implementation. Verify if the current state of the project implements the spec completely and correctly. Write your feedback to a new file named "implementation-review-N.md" in the feature folder so I can feed it back to my developer so they can fix their work. Replace N in the filename with the next available number starting from 1 (because there could be multiple reviews).
If there is already a file "implementation-review.md", create a new file "implementation-review-2.md", and so on.


## Overriding remarks, feedback, instructions, if any
The following instructions override any conflicting instructions above. If there are no overriding instructions, this section is empty.

$EXTRA_INSTRUCTIONS
