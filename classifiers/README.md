Classifier definitions live here: one <name>.md per classifier
(frontmatter: name, description, labels (a YAML list), optional model;
body: its classification prompt).

Classifiers are an internal, self-edit-only primitive — there is no
agent-facing tool. Core services (e.g. monitors) call one by name to map an
event to exactly one declared label.
