---
name: zugferd-invoice
description: Use when creating, attaching, extracting, or validating a Factur-X / ZUGFeRD 2.2 hybrid electronic invoice (PDF/A-3b with embedded factur-x.xml conforming to EN 16931). Conforms to European e-invoicing standards for B2B and B2G. Generates compliant CrossIndustryInvoice XML from structured invoice data, embeds factur-x.xml into an existing visual invoice PDF with PDF/A-3 metadata and catalog /AF relationships, extracts embedded XML from incoming hybrid invoices, and validates invoice totals, tax calculations, and schema conformity. Standard library only, no external dependencies.
license: Apache-2.0
compatibility: Python 3.10+, standard library only. Agent-agnostic.
metadata:
  version: "1.0.0"
---

# ZUGFeRD / Factur-X: Hybrid E-Invoices conforming to EN 16931

A standard visual PDF invoice cannot be read reliably by automated enterprise accounting systems,
while a pure XML invoice cannot be read by human recipients without specialized viewer software.
Factur-X (in France) and ZUGFeRD 2.2 (in Germany) standardize a single hybrid format: a human-readable
PDF/A-3 document with machine-readable CrossIndustryInvoice XML (`factur-x.xml`) embedded inside it.

This skill provides a pure Python (standard library only) engine to generate compliant XML, attach it
to existing PDF invoices with valid PDF/A-3 catalog and XMP metadata, extract embedded invoices from
incoming files, and verify mathematical and structural compliance.

It requires no external Python packages, no external CLI utilities, and runs completely offline.

## Modes

- **`pack`**: Generates a valid Factur-X / ZUGFeRD 2.2 XML file (`factur-x.xml`) conforming to EN 16931
  from structured JSON invoice data.
- **`attach`**: Takes an existing visual PDF invoice and an XML invoice file, embeds the XML into the PDF
  with PDF/A-3 conformance metadata, `/AF` catalog array, and `/AFRelationship /Alternative` file specification.
- **`extract`**: Extracts the embedded `factur-x.xml` from an incoming hybrid invoice PDF to disk.
- **`verify`**: Inspects a PDF or XML invoice, checking PDF/A-3 compliance, embedded stream integrity,
  line item sums, tax calculations, and invoice totals.
- **`selftest`**: Runs the offline selftest suite validating end-to-end invoice generation, attachment,
  extraction, round-trip equality, and error handling.

## Usage

All commands run with Python 3.10+ without any virtual environment or package installation:

```bash
python3 skills/zugferd-invoice/scripts/invoice.py --selftest
```

### 1. Generating Factur-X XML (`pack`)

Pass a JSON file containing the invoice header, seller, buyer, line items, and tax rates:

```bash
python3 skills/zugferd-invoice/scripts/invoice.py pack --data invoice.json --out factur-x.xml
```

Expected JSON format:

```json
{
  "invoice_number": "INV-2026-0042",
  "issue_date": "2026-11-07",
  "currency": "EUR",
  "profile": "BASIC",
  "seller": {
    "name": "Acme Solutions GmbH",
    "vat_id": "DE123456789",
    "country": "DE",
    "city": "Munich",
    "postal_code": "80331",
    "street": "Maximilianstr. 10"
  },
  "buyer": {
    "name": "Global Logistics S.A.",
    "vat_id": "FR98765432101",
    "country": "FR",
    "city": "Paris",
    "postal_code": "75008",
    "street": "Boulevard Haussmann 25"
  },
  "items": [
    {
      "name": "Cloud Engineering Consulting",
      "quantity": 10.0,
      "unit_price": 150.00,
      "unit_code": "HUR",
      "tax_percent": 19.0
    }
  ],
  "payment": {
    "iban": "DE89370400440532013000",
    "bic": "DBEFRM44XXX",
    "reference": "INV-2026-0042"
  }
}
```

### 2. Attaching XML to an Existing Invoice PDF (`attach`)

When you already have a visual invoice PDF produced by your billing or rendering pipeline, attach
the generated XML as an official hybrid e-invoice:

```bash
python3 skills/zugferd-invoice/scripts/invoice.py attach \
    --pdf print_invoice.pdf \
    --xml factur-x.xml \
    --out hybrid_invoice.pdf \
    --profile BASIC
```

This updates the PDF with:
1. An embedded stream containing `factur-x.xml` with MIME type `text/xml`.
2. A file specification dictionary (`/Filespec`) with `/AFRelationship /Alternative`.
3. An `/AF` (Associated Files) array in the document `/Root` catalog pointing to the file specification.
4. An embedded files dictionary in the document `/Names` tree.
5. An XMP metadata stream asserting PDF/A-3b conformance (`pdfaid:part="3"`, `pdfaid:conformance="B"`)
   and Factur-X extension schema declaration.

### 3. Extracting XML from an Incoming Hybrid Invoice (`extract`)

To inspect or process an invoice received from a supplier:

```bash
python3 skills/zugferd-invoice/scripts/invoice.py extract --pdf received_invoice.pdf --out supplier_factur-x.xml
```

Prints the extracted profile, invoice number, and writes the exact XML payload.

### 4. Verifying an Invoice (`verify`)

Check both the visual wrapper and the embedded XML for structural integrity and mathematical correctness:

```bash
python3 skills/zugferd-invoice/scripts/invoice.py verify --pdf hybrid_invoice.pdf
```

Output:
```
VERIFIED hybrid_invoice.pdf profile=BASIC invoice=INV-2026-0042 total=1785.00 EUR
```

Checks performed:
- PDF carries `/AF` array and `/AFRelationship /Alternative`.
- XMP metadata contains valid PDF/A-3 and Factur-X extension namespace.
- Embedded XML decodes cleanly and matches SHA-256 digest in `/Params`.
- Line item net amounts sum up to `LineTotalAmount`.
- Tax category amounts match line items and add up to `TaxTotalAmount`.
- `GrandTotalAmount` equals net amount plus tax amount.

| Exit code | Meaning |
| --- | --- |
| 0 | Verified, generated, or extracted successfully |
| 1 | Refused: invalid mathematical totals, missing required fields, or non-compliant PDF |
| 2 | Usage error, missing argument, or file not found |
