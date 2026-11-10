---
name: zugferd-invoice
description: Use when creating, attaching, extracting, or validating a Factur-X / ZUGFeRD 2.2 hybrid electronic invoice (PDF/A-3b with embedded factur-x.xml conforming to EN 16931). Conforms to European e-invoicing standards for B2B and B2G. Generates compliant CrossIndustryInvoice XML from structured invoice data, embeds factur-x.xml into a visual invoice PDF with PDF/A-3 metadata, output intent, and catalog /AF relationships, extracts embedded XML from incoming hybrid invoices, and validates invoice totals, tax calculations, and veraPDF PDF/A-3b clause invariants. Standard library only, no external dependencies.
license: Apache-2.0
compatibility: Python 3.10+, standard library only. Agent-agnostic.
metadata:
  version: "1.0.0"
---

# ZUGFeRD / Factur-X: Hybrid E-Invoices conforming to EN 16931

A standard visual PDF invoice cannot be read reliably by automated enterprise accounting systems,
while a pure XML invoice cannot be read by human recipients without specialized viewer software.
Factur-X and ZUGFeRD 2.2 standardize a single hybrid format: a human-readable PDF/A-3 document with
machine-readable CrossIndustryInvoice XML (`factur-x.xml`) embedded inside it.

Naive attempts by AI agents to attach XML invoices fail compliance checkers (veraPDF, KoSIT):

```
# veraPDF 1.30.4, -f 3b; pypdf 7.0.0, ReportLab 5.1.0, WeasyPrint 70.0, factur-x 7.9, Python 3.12
ReportLab + pypdf add_attachment
  7 failed: 6.8-1 Subtype, 6.8-2 F/UF, 6.8-3 AFRelationship, 6.8-4 not associated,
            6.6.2.1-1 Metadata, 6.2.4.3-4 DeviceGray, 6.2.11.4.1-1 fonts
WeasyPrint plain + factur-x
  2 failed: 6.1.3-1 trailer ID, 6.2.4.3-2 DeviceRGB without output intent
WeasyPrint pdf/a-3b + factur-x
  compliant, 146 rules passed
```

This skill provides a pure Python standard library engine to generate compliant EN 16931 XML,
attach it to existing PDF invoices with exact PDF/A-3b catalog and XMP metadata, extract embedded
invoices from incoming files, and verify mathematical and structural compliance against veraPDF clauses.

## Modes

- **`pack`**: Generates a valid Factur-X / ZUGFeRD 2.2 XML file (`factur-x.xml`) conforming to EN 16931
  from structured JSON invoice data and attaches it to a PDF.
- **`attach`**: Takes an existing visual PDF invoice and an XML invoice file, embeds the XML into the PDF
  with PDF/A-3b conformance metadata, `/AF` catalog array, and `/AFRelationship /Alternative`.
- **`extract`**: Extracts the embedded `factur-x.xml` from an incoming hybrid invoice PDF to disk.
- **`verify`**: Inspects a PDF or XML invoice, checking veraPDF PDF/A-3b clauses (`6.8-1..4`, `6.2.4.3-2`,
  `6.1.3-1`, `6.6.2.1-1`), line item sums, and KoSIT EN 16931 business rules (`BR-01`, `BR-CO-15`).
- **`selftest`**: Runs the offline selftest suite validating end-to-end invoice generation, attachment,
  extraction, round-trip equality, and exact clause refusals.

## Usage

All commands run with Python 3.10+ without external packages:

```bash
python3 skills/zugferd-invoice/scripts/invoice.py --selftest
```

### 1. Generating Factur-X XML and Packing (`pack`)

Pass a JSON file containing the invoice header, seller, buyer, line items, and tax rates:

```bash
python3 skills/zugferd-invoice/scripts/invoice.py pack \
    --pdf print_invoice.pdf \
    --data invoice.json \
    --out hybrid_invoice.pdf
```

Expected JSON format:

```json
{
  "number": "INV-2026-0042",
  "date": "2026-11-07",
  "currency": "EUR",
  "profile": "EN 16931",
  "seller": {
    "name": "Acme Solutions GmbH",
    "vat_id": "DE123456789"
  },
  "buyer": {
    "name": "Global Logistics S.A.",
    "vat_id": "FR98765432101"
  },
  "lines": [
    {
      "name": "Cloud Engineering Consulting",
      "amount": 1000.00
    }
  ],
  "net": 1000.00,
  "tax": 190.00,
  "total": 1190.00
}
```

### 2. Attaching XML to an Existing Invoice PDF (`attach`)

When you already have a visual invoice PDF produced by WeasyPrint (`pdf_variant="pdf/a-3b"`):

```bash
python3 skills/zugferd-invoice/scripts/invoice.py attach \
    --pdf print_invoice.pdf \
    --xml factur-x.xml \
    --out hybrid_invoice.pdf \
    --profile "EN 16931"
```

This updates the PDF with:
1. `Clause 6.8-1`: Embedded stream with `/Subtype /text#2Fxml`.
2. `Clause 6.8-2` & `6.8-3`: File specification dictionary with `/F`, `/UF` and `/AFRelationship /Alternative`.
3. `Clause 6.8-4`: Document Catalog `/AF` array pointing to the file specification.
4. `Clause 6.6.2.1-1`: XMP metadata stream asserting PDF/A-3b (`pdfaid:part="3"`, `pdfaid:conformance="B"`)
   and Factur-X extension schema declaration.
5. `Clause 6.2.4.3-2`: Preserved or injected `/OutputIntents` dictionary with ICC profile.
6. `Clause 6.1.3-1`: Trailer `/ID` array with matching file identifiers.

### 3. Extracting XML from an Incoming Hybrid Invoice (`extract`)

To inspect or process an invoice received from a supplier:

```bash
python3 skills/zugferd-invoice/scripts/invoice.py extract received_invoice.pdf --out supplier_factur-x.xml
```

Prints the extracted profile, invoice number, and writes the exact XML payload.

### 4. Verifying an Invoice (`verify`)

Check both the visual wrapper and the embedded XML for structural integrity and mathematical correctness:

```bash
python3 skills/zugferd-invoice/scripts/invoice.py verify hybrid_invoice.pdf
```

Output:
```
VALID Factur-X EN 16931 (sha256=a1b2c3d4e5f60718...)
```

Invariants checked:
- `veraPDF [6.1.3-1]`: Trailer contains matching `/ID` array.
- `veraPDF [6.2.4.3-2]`: Color output intent present for DeviceRGB.
- `veraPDF [6.8-1]`: Embedded stream carries `/Subtype /text#2Fxml`.
- `veraPDF [6.8-2]`: File specification contains `/UF` key.
- `veraPDF [6.8-3]`: File specification carries `/AFRelationship /Alternative`.
- `veraPDF [6.8-4]`: Document catalog contains `/AF` array.
- `veraPDF [6.6.2.1-1]`: Valid PDF/A-3b and Factur-X XMP extension schema.
- `KoSIT [BR-01]`: Guideline URN is `urn:cen.eu:en16931:2017#compliant#urn:factur-x.eu:1p0:en16931`.
- `KoSIT [BR-CO-15]`: Grand total equals net plus tax.

| Exit code | Meaning |
| --- | --- |
| 0 | Verified, generated, or extracted successfully |
| 1 | Refused: invalid mathematical totals, missing required fields, or non-compliant PDF |
| 2 | Usage error, missing argument, or file not found |
