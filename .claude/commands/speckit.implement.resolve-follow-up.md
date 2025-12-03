---
description: Resolve feedback received on a speckit feature implementation
---

We use speckit as workflow to implement features in this project. The feature we are currently working has a specific feature folder under specs/ with the same name as the current branch. In the feature folder, there is a spec.md file that contains the specification of the feature.

We just finished the resolution of the feedback of a quality review of the implementation of the feature. However, there are some follow-up questions/feedback:


## Feedback

$ARGUMENTS

## Workflow

If one or more github issues are referenced in the feedback, make sure to read them first: title, description and all the comments.

For each point in the feedback, follow a structured workflow to resolve it:
0. Analyze the feedback and context. If anything is unclear, ask clarifying questions first and wait for my answers.
1. Update/implement the tests (TDD)
2. Refactor code
3. Refactor docs. They are equally important as code. Ensure ALL docs are adapted accordingly so they reflect the current state of the codebase.
4. Validate: type check, lint, run tests, ...

Delegate as much as possible to subagents (even analysis), so that you can keep your context clean and you can focus on your task as project manager and orchestrator, but ensure that the final result is a coherent, working solution. Launch subagents in parallel if their tasks don't conflict.

## Output
Append a report of this "follow-up session" to the existing file "implementation-review-N-resolution.md" that has the highest number N. The goal is to have a trace of any decisions we make while resolving the feedback. The report should contain the following:
- The literal ARGUMENTS text passed to you when invoking this command.
- A log of any further conversation we have while you are resolving the feedback. Include ALL my prompts and your output LITERALY, e.g. your clarifying questions and my answers as well.
