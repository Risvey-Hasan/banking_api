TX_COLUMNS = """
    transaction_id,
    kind::text   AS kind,
    type::text   AS type,
    status::text AS status,
    reverses_id  AS reverses_transaction_id,
    account_id, amount, currency, device_id,
    accepted_at, processed_at, failed_attempts
"""