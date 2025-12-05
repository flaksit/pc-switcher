---
description: Resolve feedback received on a speckit feature implementation
---

We use speckit as workflow to implement features in this project. The feature we are currently working has a specific feature folder under specs/ with the same name as the current branch. In the feature folder, there is a spec.md file that contains the specification of the feature.

You just finished the implementation of the feature. My quality assurance analyst reviewed your implementation against the specs. You find their feedback in the file "implementation-review-N*.md" in the feature folder. Make sure to read only the file with the highest number N.
First, critically read through their feedback. For each point in the feedback, ensure you understand the underlying issue. If you disagree with any point, discuss it with me. Come back to me and ask clarifying questions or ask for deciding if needed (e.g. between multiple possible solutions).

Then, when all your questions are answered, fix your implementation according to the feedback. Ensure all points raised in the feedback are addressed and that the implementation fully complies with the specifications outlined in "spec.md".

For each point in the feedback, follow a structured workflow to resolve it:
0. Analyze the question and context. If anything is unclear, ask clarifying questions first and wait for my answers.
1. Update/implement the tests (TDD)
2. Refactor code
3. Refactor docs. They are equally important as code. Ensure ALL docs are adapted accordingly so they reflect the current state of the codebase.
4. Validate: type check, lint, run tests, ...

Delegate as much as possible to subagents (even analysis), so that you can keep your context clean and you can focus on your task as project manager and orchestrator, but ensure that the final result is a coherent, working solution. Launch subagents in parallel if their tasks don't conflict.

### Output
Write a report to a new file "implementation-review-N-resolution.md", where N is the same number as of the review file you read. The goal is to have a trace of any decisions we make while resolving the feedback. The report should contain the following:
- The literal ARGUMENTS text passed to you when invoking this command.
- A log of any further conversation we have while you are resolving the feedback. This includes  your clarifying questions and my answers, but we could get into deeper conversation about some of the feedback given. Capture my prompts, your output and questions, my responses, ... literally.


## Overriding remarks, feedback, instructions, if any

The following instructions override any conflicting instructions above or in the review file. If there are no overriding instructions, this section is empty.

$ARGUMENTS
