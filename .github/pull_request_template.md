## What does this change?

<!-- One or two sentences. What behaviour is different after this PR? -->

## Why?

<!-- The problem being solved. Link an issue if one exists. -->

## How was it verified?

<!--
Name the commands you ran and what they reported. "Tests pass" is not
verification; a command exiting 0 is not proof the intended outcome happened.
-->

- [ ] `make lint` and `make format-check`
- [ ] `make test-fast` (unit + contract), run on its own
- [ ] `make test-cdk` if anything under `deploy-cdk/` changed
- [ ] `make test-frontend` if anything under `dashboard-react/` changed
- [ ] `npx cdk synth --all` is clean, including cdk-nag, if infrastructure changed

## Checklist

- [ ] No AWS account IDs, ARNs with real accounts, credentials, or internal
      hostnames added. Placeholders only (`123456789012`).
- [ ] No real customer data or PII in fixtures or examples.
- [ ] New source files carry an SPDX header.
- [ ] Docs updated if behaviour, configuration, or deployment changed.
- [ ] I have read [CONTRIBUTING.md](../CONTRIBUTING.md).

## Anything reviewers should look at closely?

<!-- Trade-offs you made, alternatives you rejected, parts you are unsure about. -->
