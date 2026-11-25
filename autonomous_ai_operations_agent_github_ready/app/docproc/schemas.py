"""Document types and the fields the workflow looks for. Edit this file to add a new type."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    aliases: tuple[str, ...]  # label text that may appear before the value, e.g. "Invoice number:"
    kind: str = "text"        # text | date | money
    required: bool = True


@dataclass(frozen=True)
class DocumentType:
    key: str
    label: str
    signals: tuple[str, ...]  # words that suggest this type
    fields: tuple[FieldSpec, ...]


DOCUMENT_TYPES = (
    DocumentType(
        key="invoice",
        label="Supplier invoice",
        signals=("invoice", "amount due", "bill to", "payment terms"),
        fields=(
            FieldSpec("invoice_number", "Invoice number", ("Invoice number", "Invoice no", "Invoice #", "Invoice No.")),
            FieldSpec("invoice_date", "Invoice date", ("Invoice date", "Date"), kind="date"),
            FieldSpec("supplier_name", "Supplier name", ("Supplier name", "Supplier", "Vendor")),
            FieldSpec("total_amount", "Total amount", ("Total amount", "Amount due", "Total"), kind="money"),
            FieldSpec("purchase_order", "Purchase order", ("Purchase order", "PO number"), required=False),
            FieldSpec("payment_terms", "Payment terms", ("Payment terms", "Terms"), required=False),
        ),
    ),
    DocumentType(
        key="onboarding_form",
        label="Employee onboarding form",
        signals=("onboarding", "joining", "employee details", "start date"),
        fields=(
            FieldSpec("employee_name", "Employee name", ("Employee name", "Full name")),
            FieldSpec("start_date", "Start date", ("Start date", "Joining date"), kind="date"),
            FieldSpec("department", "Department", ("Department",)),
            FieldSpec("manager", "Reporting manager", ("Reporting manager", "Manager")),
            FieldSpec("work_email", "Work email", ("Work email", "Email")),
            FieldSpec("phone", "Phone", ("Phone", "Mobile"), required=False),
        ),
    ),
)
