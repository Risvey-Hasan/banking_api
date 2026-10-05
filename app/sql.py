TX_COLUMNS = """
    transaction_id,
    kind::text   AS kind,
    type::text   AS type,
    status::text AS status,
    reverses_id  AS reverses_transaction_id,
    reverses_type::text AS reverses_type,
    account_id, to_account_id, claimed_account_id,
    amount, currency, device_id,
    accepted_at, processed_at, failed_attempts
"""