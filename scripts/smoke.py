import json

import httpx

BASE = "http://localhost:8000"


def show(label: str, response: httpx.Response) -> None:
    print(f"\n--- {label}  ->  {response.status_code}")
    try:
        print(json.dumps(response.json(), indent=2))
    except ValueError:
        print(response.text)


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=10.0) as client:
        show("health", client.get("/health"))

        created = client.post(
            "/accounts",
            json={"currency": "SAR", "initial_balance": "10000.00"},
        )
        show("create account", created)

        if created.status_code == 201:
            account_id = created.json()["id"]
            show("get account", client.get(f"/accounts/{account_id}"))

        show(
            "unknown currency",
            client.post("/accounts", json={"currency": "ZZZ", "initial_balance": "5"}),
        )
        show("missing account", client.get("/accounts/999999"))


if __name__ == "__main__":
    main()