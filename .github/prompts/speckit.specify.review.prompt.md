---
description: Review a speckit feature specification
---

## User Input

```text
$EXTRA_INSTRUCTIONS
```

You **MUST** consider the user input before proceeding (if not empty). The user input takes precedence over any conflicting instructions in this prompt.


We use speckit as workflow to implement features in this project. The feature we are currently working has a specific feature folder under specs/ with the same name as the current branch. In the feature folder, there is a spec.md file that contains the specification of the feature.

My analyst just wrote a detailed specification for the feature. You can find it in the file "spec.md" in the feature folder.
Review their specification on completeness, correctness, consistency. Give any other valuable feedback or ideas for substantial improvements you might have. Don't change the specification itself. Write your feedback to a new file "spec-review.md" in the feature folder so I can feed your feedback to the analyst to fix/improve the specification.
If there is already a file "spec-review.md", create a new file "spec-review-2.md", and so on.


## Overriding remarks, feedback, instructions, if any
The following instructions override any conflicting instructions above. If there are no overriding instructions, this section is empty.

$EXTRA_INSTRUCTIONS
