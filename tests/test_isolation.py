"""Kullanıcıların birbirinin verisine erişemediğini doğrulayan testler (eski sürümdeki açıklar)."""

import pytest

from app import tabular
from tests.conftest import login_client, make_user, register, wait_ready

CSV = "urun,adet\nelma,10\narmut,5\nelma,3\n".encode()


def _setup_users(client):
    register(client)  # 1 numaralı kullanıcı (yönetici)
    users = {}
    for i in range(2, 11):  # 2..10 numaralı kullanıcılar
        users[i] = make_user(client, f"K{i}", f"k{i}@firma.com")
    return users


def test_user_1_upload_does_not_touch_user_10_tables(client):
    users = _setup_users(client)
    assert users[10]["id"] == 10
    u10 = login_client("k10@firma.com")
    assert u10.post("/api/datasets", files={"file": ("maaslar.csv", b"isim,maas\nali,100\n")}).status_code == 200

    assert client.post("/api/datasets", files={"file": ("satislar.csv", CSV)}).status_code == 200
    assert [d["table_name"] for d in client.get("/api/datasets").json()] == ["satislar"]
    assert [d["table_name"] for d in u10.get("/api/datasets").json()] == ["maaslar"]
    assert tabular.run_query(10, "SELECT * FROM maaslar").shape == (1, 2)


def test_query_cannot_reach_other_users_data(client):
    _setup_users(client)
    u2 = login_client("k2@firma.com")
    u2.post("/api/datasets", files={"file": ("maaslar.csv", b"isim,maas\nali,100\n")})
    client.post("/api/datasets", files={"file": ("satislar.csv", CSV)})

    with pytest.raises(tabular.DataError):
        tabular.run_query(1, "SELECT * FROM maaslar")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM satislar; DROP TABLE satislar",
    "DELETE FROM satislar",
    "WITH x AS (SELECT 1) DELETE FROM satislar",
    "SELECT load_extension('x')",
    "SELECT * FROM pragma_table_info('satislar') WHERE 0; ATTACH 'x' AS y",
])
def test_dangerous_sql_is_blocked(client, sql):
    register(client)
    client.post("/api/datasets", files={"file": ("satislar.csv", CSV)})
    with pytest.raises(tabular.DataError):
        tabular.run_query(1, sql)
    assert len(tabular.run_query(1, "SELECT * FROM satislar")) == 3


def test_private_documents_hidden_shared_documents_visible(client):
    register(client)
    make_user(client, "Ali", "ali@firma.com")
    make_user(client, "Veli", "veli@firma.com")
    ali, veli = login_client("ali@firma.com"), login_client("veli@firma.com")

    private = ali.post("/api/documents", files={"file": ("ozel.txt", "Ali'nin gizli notu.".encode())}).json()
    shared = client.post("/api/documents", files={"file": ("yonetmelik.txt", "Şirket yönetmeliği metni.".encode())},
                         data={"shared": "true"}).json()
    wait_ready(ali, private["id"])
    wait_ready(client, shared["id"])

    veli_docs = {d["filename"]: d for d in veli.get("/api/documents").json()}
    assert "ozel.txt" not in veli_docs
    assert veli_docs["yonetmelik.txt"]["shared"] and not veli_docs["yonetmelik.txt"]["can_delete"]
    assert veli.delete(f"/api/documents/{private['id']}").status_code == 404
    assert veli.delete(f"/api/documents/{shared['id']}").status_code == 404
    # Yönetici bile başkasının kişisel dokümanını göremez.
    assert "ozel.txt" not in {d["filename"] for d in client.get("/api/documents").json()}


def test_only_admin_can_share(client):
    register(client)
    make_user(client, "Ali", "ali@firma.com")
    ali = login_client("ali@firma.com")
    r = ali.post("/api/documents", files={"file": ("a.txt", "Bir metin parçası burada.".encode())},
                 data={"shared": "true"})
    assert r.status_code == 400


def test_chats_are_private(client, ollama):
    register(client)
    make_user(client, "Ali", "ali@firma.com")
    chat_id = client.post("/api/chats").json()["id"]
    ali = login_client("ali@firma.com")
    assert ali.get(f"/api/chats/{chat_id}/messages").status_code == 404
    assert ali.delete(f"/api/chats/{chat_id}").status_code == 404
    # Başkasının sohbet numarasıyla soru sorulursa yeni bir sohbet açılır, oradakine yazılmaz.
    r = ali.post("/api/ask", json={"question": "merhaba", "chat_id": chat_id})
    assert f'"chat_id": {chat_id}' not in r.text
    assert client.get(f"/api/chats/{chat_id}/messages").json() == []


def test_deleting_user_removes_their_data(client):
    register(client)
    ali_info = make_user(client, "Ali", "ali@firma.com")
    ali = login_client("ali@firma.com")
    doc = ali.post("/api/documents", files={"file": ("not.txt", "Ali'nin notları burada yazıyor.".encode())}).json()
    wait_ready(ali, doc["id"])
    ali.post("/api/datasets", files={"file": ("satislar.csv", CSV)})

    assert client.delete(f"/api/admin/users/{ali_info['id']}").status_code == 200
    assert ali.get("/api/auth/me").status_code == 401
    assert not (tabular._user_db_path(ali_info["id"])).exists()
