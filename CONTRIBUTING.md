# Contributing Guidelines

Thank you for your interest in contributing to our project. Whether it's a bug
report, new feature, correction, or additional documentation, we greatly value
feedback and contributions from our community.

Please read through this document before submitting any issues or pull requests
to ensure we have all the necessary information to effectively respond to your
bug report or contribution.

## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest
features.

When filing an issue, please check existing open, or recently closed, issues to
make sure somebody else hasn't already reported the issue. Please try to
include as much information as you can. Details like these are incredibly
useful:

- A reproducible test case or series of steps
- The version of our code being used
- Any modifications you've made relevant to the bug
- Anything unusual about your environment or deployment

## Contributing via Pull Requests

Contributions via pull requests are much appreciated. Before sending us a pull
request, please ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure
   someone else hasn't addressed the problem already.
3. You open an issue to discuss any significant work — we would hate for your
   time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing.
   If you also reformat all the code, it will be hard for us to focus on your
   change.
3. Ensure local tests pass (`make check` from the repository root).
4. Commit to your fork using clear commit messages.
5. Send us a pull request, answering any default questions in the pull request
   interface.
6. Stay involved in the conversation on the pull request. This repository has
   no hosted CI, so reviewers rely on what you ran locally: fill in the
   pull request template's "How was it verified?" section with the commands
   and what they reported.

GitHub provides additional documentation on
[forking a repository](https://help.github.com/articles/fork-a-repo/) and
[creating a pull request](https://help.github.com/articles/creating-a-pull-request/).

## Reading the code: provenance markers

Comments, docstrings and test failure messages throughout this repository cite
short identifiers from the project's internal design history — `LGR-819980f-1`,
`p64 R2.5`, `Task 10.7` and similar — and sometimes paths under a `.kiro/`
directory, such as `.kiro/specs/p66-glue-catalog-target-selection/requirements.md`.
Each records which decision or requirement a piece of code implements.

Neither the records nor the `.kiro/` directory is part of this repository, so
these references do not resolve here. Nothing depends on them: they are provenance labels, not
behaviour, and no test asserts on one. Where such a decision constrains how the
code may be changed, the substance is written out in full in the relevant
document under `docs/` rather than left to the identifier — so if a comment
cites an id you cannot resolve, the reason it stands for is in the docs.

Treat them as you would a reference to a closed ticket. There is no need to add
new ones in a contribution.

## Finding contributions to work on

Looking at the existing issues is a great way to find something to contribute
on. Looking at any 'help wanted' issues is a great place to start.

## Code of Conduct

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.

## Security issue notifications

If you discover a potential security issue in this project we ask that you
notify AWS/Amazon Security via our
[vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/).
Please do **not** create a public GitHub issue.

## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to
confirm the licensing of your contribution.
