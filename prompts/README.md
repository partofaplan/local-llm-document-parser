# Prompt files

Each `*.yaml` file in this directory defines one extraction task. The service re-reads the directory on
every request, so edited files take effect immediately. That includes a mounted ConfigMap, which the
kubelet refreshes within about a minute.

```yaml
name: invoice                 # defaults to the file name; used as the `prompt` form value
description: Shown in the UI and /api/prompts.
model: qwen2.5:7b             # optional; overrides DOCPARSER_MODEL for this prompt
system: |                     # optional system prompt
  You are a meticulous document analyst...
instructions: |               # task-specific guidance placed before the document
  The document is an invoice...
options:                      # optional Ollama options, e.g. a larger context for long documents
  num_ctx: 16384
fields:                       # what to extract; becomes a JSON schema that constrains the model
  - name: invoice_number      # identifier: letters, digits, underscore
    type: string              # string | number | integer | boolean | date | array | object
    description: Invoice or bill number
    required: true            # reported in validation_errors when missing
  - name: status
    enum: [paid, unpaid, overdue]
  - name: vendor
    type: object
    fields:
      - name: name
      - name: address
  - name: line_items
    type: array
    items:                    # element type (default: string)
      type: object
      fields:
        - name: description
        - name: amount
          type: number
output_pdf:                   # optional: enables /api/fill
  template: invoice_summary.pdf   # fillable (AcroForm) PDF, relative to this file
  field_map:                      # PDF form field -> result path, or a template with {placeholders}
    VendorName: vendor.name
    FirstItem: line_items.0.description
    Total: "{total_amount} {currency}"
```

Rules:

- **Optional fields are nullable.** The model returns `null` rather than guessing.
- **Unmapped form fields match by name.** A form field such as `InvoiceNumber` is filled from `invoice_number`, because names are compared ignoring case and punctuation. Only list fields in `field_map` when the names differ.
- **Checkboxes** are ticked when the value is `true` or `"yes"`.
- **Lists** go into multiline text fields one item per line, wrapped to the field width.
- **Finding form field names:** `POST /api/templates/inspect` with a `template` file lists them.

## Adding a prompt for Kubernetes

Add the new file (and any PDF template) to the `files:` list in [kustomization.yaml](kustomization.yaml),
then re-apply the overlay. ConfigMaps are limited to 1 MiB in total, so larger templates should be
baked into the image instead: they are copied from this directory at build time.
