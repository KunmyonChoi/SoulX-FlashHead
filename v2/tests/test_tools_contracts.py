"""Contract files, the input validator, the consent reader and GET /api/tools."""
import textwrap

import pytest
import yaml

from agentkit import config
from agentkit.contracts import ToolContract
from agentkit.tools import ContractError, InputError, build_gateway, is_consent, load_contracts, read_consent, validate
from agentkit.tools import catalog
from agentkit.tools.validate import check_schema

SHOP = ["verify_customer", "get_order", "track_delivery", "request_refund", "handoff_to_human"]
V1 = ["get_weather", "search_news", "get_news_detail", "search_geeknews", "read_geeknews", "web_search",
      "search_wikipedia", "list_knowledge_by_date", "search_knowledge"]
CAFE = ["get_menu", "search_menu", "get_cart", "add_to_cart", "remove_from_cart", "apply_coupon", "request_payment"]
WEB = ["web_task", "web_task_answer", "web_task_confirm"]      # office-assistant: the browser service (v2/browser)
# the dummy clinic, burger shop and airline (agentkit/dummy), reached through python adapters
CLINIC = ["find_patient", "list_available_slots", "book_appointment", "cancel_appointment"]
DRIVE = ["search_menu", "get_cart", "add_to_cart", "remove_from_cart", "submit_order"]
FLIGHT = ["verify_booking", "get_booking", "search_alternative_flights", "quote_change_fee", "change_flight"]


# ---- the contracts ----------------------------------------------------------------------


GOOGLE = ["list_events", "find_free_time", "create_event", "mail_briefing", "search_mail", "read_mail", "send_mail", "create_draft", "send_draft",
          "list_mail", "cleanup_mail", "undo_cleanup", "cancel_send"]
PACKS = {"shop": SHOP[:4], "common": SHOP[4:] + ["calculate"], "lookup": V1, "cafe": CAFE, "web": WEB, "clinic": CLINIC, "burger": DRIVE, "airline": FLIGHT,
         "google": GOOGLE}


def test_all_contracts_load_and_are_complete():
    contracts = load_contracts()
    assert sorted(contracts) == sorted(f"{pack}.{name}" for pack, names in PACKS.items() for name in names)
    assert all(c.id == key and c.pack == key.split(".")[0] and c.name == key.split(".")[1] for key, c in contracts.items())
    for name, contract in contracts.items():
        assert isinstance(contract, ToolContract)
        assert contract.description and contract.filler, name
        assert contract.input_schema["type"] == "object", name
        assert contract.output_schema, name
        assert contract.timeout_s > 0, name


def test_shop_contracts_carry_the_safety_settings():
    c = catalog(load_contracts(), ["shop", "common"])
    assert [c[name].effect for name in SHOP] == ["read", "read", "read", "write", "write"]
    assert c["verify_customer"].grants == ["verified"] and c["verify_customer"].requires == []
    for name in ("get_order", "track_delivery", "request_refund"):
        assert c[name].requires == ["verified"], name
    assert c["handoff_to_human"].requires == [] and c["handoff_to_human"].confirm_template is None
    refund = c["request_refund"]
    assert refund.confirm_template == "{product_name} {amount}원 환불을 접수할까요?"
    assert refund.idempotency_fields == ["order_id"] and refund.retries == 0
    assert c["verify_customer"].pii_input == ["phone_last4"]
    assert "customer_name" in c["get_order"].pii_output and "address" in c["get_order"].pii_output
    assert all(c[name].adapter.kind == "dummy" and c[name].adapter.target == f"shop:{name}" for name in SHOP)


def test_v1_contracts_match_v1_specs():
    import sys
    sys.path.append(str(config.REPO_ROOT))
    import realtime_tools
    contracts = catalog(load_contracts(), ["lookup"])
    for spec in realtime_tools.TOOL_SPECS:
        function = spec["function"]
        contract = contracts[function["name"]]
        assert contract.description == function["description"]
        assert contract.input_schema == function["parameters"]
        assert contract.filler == realtime_tools.FILLERS[function["name"]]
        assert contract.effect == "read" and contract.adapter.kind == "python"
        assert contract.adapter.target == f"agentkit.tools.v1_lookup:{function['name']}"
    knowledge = contracts["list_knowledge_by_date"]
    assert knowledge.input_schema["required"] == ["date"] and knowledge.effect == "read"


# ---- loading and checking ----------------------------------------------------------------------

GOOD = """
name: sample
description: 시험용 도구
effect: write
input_schema:
  type: object
  properties:
    order_id: {type: string}
  required: [order_id]
confirm_template: "{order_id}를 처리할까요?"
idempotency_fields: [order_id]
adapter: {kind: dummy, target: "shop:request_refund"}
"""


def write(folder, name, text):
    (folder / f"{name}.yaml").write_text(textwrap.dedent(text), encoding="utf-8")


def test_good_contract_loads(tmp_path):
    write(tmp_path, "sample", GOOD)
    assert load_contracts(tmp_path)["sample"].idempotency_fields == ["order_id"]
    assert build_gateway(tools_dir=tmp_path).names() == ["sample"]


@pytest.mark.parametrize("change,expected", [
    (lambda d: d.update(name="other"), "name"),
    (lambda d: d.update(effect="delete"), "effect"),
    (lambda d: d.pop("description"), "description"),
    (lambda d: d.pop("adapter"), "adapter"),
    (lambda d: d.update(adapter={"kind": "soap", "target": "x"}), "adapter.kind"),
    (lambda d: d["input_schema"].update(type="array"), "input_schema.type"),
    (lambda d: d["input_schema"].update(required=["nope"]), "input_schema.required"),
    (lambda d: d["input_schema"]["properties"].update(order_id={"type": "uuid"}), "input_schema.properties.order_id.type"),
    (lambda d: d.update(idempotency_fields=["nope"]), "idempotency_fields"),
    (lambda d: d.update(pii_input=["nope"]), "pii_input"),
    (lambda d: d.update(effect="read"), "confirm_template"),
    (lambda d: d.update(timeout_s=0), "timeout_s"),
])
def test_wrong_contract_names_file_and_item(tmp_path, change, expected):
    data = yaml.safe_load(GOOD)
    change(data)
    (tmp_path / "sample.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ContractError) as error:
        load_contracts(tmp_path)
    assert any(p.startswith("sample.yaml") and expected in p for p in error.value.problems), error.value.problems


READ = """
name: view
description: 주문 내역을 본다.
effect: read
input_schema: {type: object, properties: {}}
display: {kind: cart}
adapter: {kind: dummy, target: "cafe:get_cart"}
"""


def with_display(tmp_path, display, view=READ):
    data = yaml.safe_load(GOOD)
    data["display"] = display
    (tmp_path / "sample.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    if view:
        write(tmp_path, "view", view)
    return tmp_path


def test_display_is_read_from_the_contract_file(tmp_path):
    contracts = load_contracts(with_display(tmp_path, {"kind": "receipt", "path": "order", "refresh": ["view"]}))
    display = contracts["sample"].display
    assert (display.kind, display.path, display.refresh) == ("receipt", "order", ["view"])
    assert (contracts["view"].display.kind, contracts["view"].display.path, contracts["view"].display.refresh) == ("cart", "", [])
    assert build_gateway(tools_dir=tmp_path).contract("sample").display.refresh == ["view"]
    assert load_contracts(with_display(tmp_path, None))["sample"].display is None


@pytest.mark.parametrize("display,view,expected", [
    ({"kind": "popup"}, READ, "display.kind"),
    ({"path": "cart"}, READ, "display.kind"),
    ("cart", READ, "display"),
    ({"kind": "cart", "refresh": "view"}, READ, "display.refresh"),
    ({"kind": "cart", "refresh": ["nope"]}, READ, "display.refresh: 'nope'"),
    ({"kind": "cart", "refresh": ["sample"]}, READ, "display.refresh: 자기 자신"),
    ({"kind": "cart", "refresh": ["view"]}, READ.replace("effect: read", "effect: write"), "조회 도구"),
    ({"kind": "cart", "refresh": ["view"]}, READ.replace("display: {kind: cart}\n", ""), "display가 없어"),
    ({"kind": "cart", "refresh": ["view"]},
     READ.replace("properties: {}", "properties: {q: {type: string}}, required: [q]"), "인자 없이"),
])
def test_wrong_display_names_file_and_item(tmp_path, display, view, expected):
    with pytest.raises(ContractError) as error:
        load_contracts(with_display(tmp_path, display, view))
    assert any(p.startswith("sample.yaml") and expected in p for p in error.value.problems), error.value.problems


def test_object_properties_are_accepted_and_handed_on():
    schema = {"type": "object", "properties": {
        "item_id": {"type": "string"},
        "options": {"type": "object", "description": "옵션", "properties": {"size": {"type": "string", "enum": ["a"]}}}},
        "required": ["item_id"]}
    assert check_schema(schema) == []
    assert check_schema({"type": "object", "properties": {"a": {"type": "string", "properties": {}}}})  # only for objects
    assert validate(schema, {"item_id": "x", "options": {"size": "큰 거", "more": 1}}) == {
        "item_id": "x", "options": {"size": "큰 거", "more": 1}}                    # the inside is the system's to read
    assert validate(schema, {"item_id": "x", "options": '{"size": "a"}'})["options"] == {"size": "a"}
    assert validate(schema, {"item_id": "x", "options": None}) == {"item_id": "x"}
    for wrong in ("아이스", ["a"], 3, "[1]", "{broken"):
        with pytest.raises(InputError) as error:
            validate(schema, {"item_id": "x", "options": wrong})
        assert [name for name, _ in error.value.problems] == ["options"]


def test_unreadable_yaml_and_all_problems_at_once(tmp_path):
    (tmp_path / "broken.yaml").write_text("name: [unclosed", encoding="utf-8")
    write(tmp_path, "sample", GOOD.replace("effect: write", "effect: nope"))
    with pytest.raises(ContractError) as error:
        load_contracts(tmp_path)
    assert {p.split(":")[0] for p in error.value.problems} == {"broken.yaml", "sample.yaml"}


@pytest.mark.parametrize("adapter,expected", [
    ({"kind": "dummy", "target": "bank:transfer"}, "더미 시스템"),
    ({"kind": "dummy", "target": "shop:delete_order"}, "delete_order"),
    ({"kind": "python", "target": "no_such_module_xyz:fn"}, "no_such_module_xyz"),
    ({"kind": "python", "target": "agentkit.tools.v1_lookup:nope"}, "nope"),
    ({"kind": "python", "target": "just_a_name"}, "패키지.모듈:함수"),
    ({"kind": "http", "target": "ftp://example"}, "http://"),
    ({"kind": "replay", "target": "missing.jsonl"}, "missing.jsonl"),
])
def test_wrong_adapter_target_is_caught_when_the_gateway_is_built(tmp_path, adapter, expected):
    data = yaml.safe_load(GOOD)
    data["adapter"] = adapter
    (tmp_path / "sample.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ContractError) as error:
        build_gateway(tools_dir=tmp_path)
    assert "sample.yaml" in str(error.value) and expected in str(error.value)


def test_adapter_override_switches_the_connection_without_touching_files(tmp_path, monkeypatch):
    gateway = build_gateway(adapter_overrides={"get_order": {"kind": "http", "target": "http://127.0.0.1:7895/shop/get_order"}})
    assert gateway.contract("get_order").adapter.kind == "http"
    assert gateway.contract("track_delivery").adapter.kind == "dummy"
    assert load_contracts()["shop.get_order"].adapter.kind == "dummy"
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text("track_delivery: {kind: http, target: 'http://127.0.0.1:7895/shop/track_delivery'}\n")
    monkeypatch.setenv("AGENTKIT_TOOL_OVERRIDES", str(overrides))
    assert build_gateway().contract("track_delivery").adapter.kind == "http"
    with pytest.raises(ContractError):
        build_gateway(adapter_overrides={"no_such_tool": {"kind": "dummy", "target": "shop:get_order"}})


# ---- input validation --------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "description": "주문번호"},
        "count": {"type": "integer", "minimum": 1, "maximum": 5},
        "ratio": {"type": "number"},
        "gift": {"type": "boolean"},
        "size": {"type": "string", "enum": ["S", "M", "L"]},
        "last4": {"type": "string", "pattern": r"^\d{4}$"},
    },
    "required": ["order_id"],
}


def test_validate_accepts_and_cleans():
    assert validate(SCHEMA, {"order_id": "A1", "count": 2, "ratio": 0.5, "gift": True, "size": "M", "last4": "1234"}) == {
        "order_id": "A1", "count": 2, "ratio": 0.5, "gift": True, "size": "M", "last4": "1234"}
    # lossless conversions of what models send
    assert validate(SCHEMA, {"order_id": 1001, "count": "3", "ratio": "1.5", "gift": "true", "last4": 1234}) == {
        "order_id": "1001", "count": 3, "ratio": 1.5, "gift": True, "last4": "1234"}
    assert validate(SCHEMA, {"order_id": "A1", "count": 2.0})["count"] == 2
    # unknown properties are dropped, optional nulls are absent
    assert validate(SCHEMA, {"order_id": "A1", "extra": 1, "size": None}) == {"order_id": "A1"}


@pytest.mark.parametrize("arguments,field", [
    ({}, "order_id"),
    ({"order_id": None}, "order_id"),
    ({"order_id": ["A1"]}, "order_id"),
    ({"order_id": True}, "order_id"),
    ({"order_id": "A1", "count": "many"}, "count"),
    ({"order_id": "A1", "count": 2.5}, "count"),
    ({"order_id": "A1", "count": True}, "count"),
    ({"order_id": "A1", "count": 0}, "count"),
    ({"order_id": "A1", "count": 6}, "count"),
    ({"order_id": "A1", "ratio": "x"}, "ratio"),
    ({"order_id": "A1", "gift": "maybe"}, "gift"),
    ({"order_id": "A1", "gift": 1}, "gift"),
    ({"order_id": "A1", "size": "XL"}, "size"),
    ({"order_id": "A1", "last4": "12a4"}, "last4"),
])
def test_validate_refuses(arguments, field):
    with pytest.raises(InputError) as error:
        validate(SCHEMA, arguments)
    assert [name for name, _ in error.value.problems] == [field]
    assert field in str(error.value)


def test_validate_reports_every_problem_and_uses_the_description():
    with pytest.raises(InputError) as error:
        validate(SCHEMA, {"count": 9, "size": "XL"})
    assert [name for name, _ in error.value.problems] == ["order_id", "count", "size"]
    assert "order_id(주문번호)" in str(error.value)
    with pytest.raises(InputError):
        validate(SCHEMA, "A1001")
    with pytest.raises(InputError):
        validate(dict(SCHEMA, additionalProperties=False), {"order_id": "A1", "extra": 1})


def test_check_schema():
    assert check_schema(SCHEMA) == []
    assert check_schema({"type": "object"}) == []
    assert check_schema({"type": "object", "properties": {"a": {"type": "string", "format": "email"}}})
    assert check_schema({"type": "object", "properties": {"a": {"type": "string", "pattern": "("}}})


# ---- consent -----------------------------------------------------------------------------------

YES = ["네", "네.", "네네", "네, 맞아요", "예", "예 접수해 주세요", "응", "응 그래", "맞아요", "맞습니다", "그래요",
       "좋아요", "좋습니다", "그렇게 해 주세요", "네 환불해 주세요", "환불해주세요", "접수해 주세요", "진행해 주세요",
       "부탁드립니다", "네 부탁드려요", "오케이", "알겠습니다 진행하세요", "동의합니다", "네, 감사합니다",
       "yes", "Yes, please", "ok", "okay go ahead", "sure", "yeah do it"]
NO = ["아니요", "아니 됐어요", "아뇨", "취소해 주세요", "취소할게요", "잠깐만요", "잠시만요", "하지 마세요", "하지 말아 주세요",
      "환불 말고 교환해 주세요", "안 할래요", "안 해도 돼요", "네, 아니 잠깐만요", "됐어요", "나중에 할게요",
      "좀 더 생각해 볼게요", "싫어요", "그만할게요", "글쎄요", "잘 모르겠어요", "필요 없어요",
      "no", "No thanks", "don't", "please don't", "cancel it", "wait", "not now", "hold on"]
UNCLEAR = ["", "   ", "네?", "예?", "얼마라고요?", "환불되는 데 며칠 걸려요?", "그게 언제 배송된 거였죠", "음", "어",
           "금액을 다시 말해 주세요", "다시 설명해 주세요", "상담원 연결해 주세요", "배송지를 확인해 주세요",
           "네이버페이로 결제했어요", "예약 주문이었어요", "응답이 없네요", "이어폰이요", "그렇네요", "what?", "how much is it"]


@pytest.mark.parametrize("text", YES)
def test_consent_yes(text):
    assert read_consent(text) == "yes" and is_consent(text)


@pytest.mark.parametrize("text", NO)
def test_consent_no(text):
    assert read_consent(text) == "no" and not is_consent(text)


@pytest.mark.parametrize("text", UNCLEAR)
def test_consent_unclear(text):
    assert read_consent(text) == "unclear" and not is_consent(text)


def test_consent_reads_none_as_unclear():
    assert read_consent(None) == "unclear"


# ---- REST --------------------------------------------------------------------------------------


def test_api_tools():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agentkit.tools.api import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    body = client.get("/api/tools").json()
    assert body["errors"] == [] and [t["name"] for t in body["tools"]] == sorted(SHOP + ["calculate"] + V1 + CAFE + WEB + CLINIC + DRIVE + FLIGHT + GOOGLE)
    assert {t["id"] for t in body["tools"]} >= {"cafe.search_menu", "burger.search_menu", "common.handoff_to_human"}
    by_name = {t["id"].split(".")[1] if t["pack"] != "burger" else t["id"]: t for t in body["tools"]}
    assert client.get("/api/tools/cafe.add_to_cart").json()["pack"] == "cafe"
    assert client.get("/api/tools/get_order").json()["id"] == "shop.get_order"          # a bare name only one pack has
    assert client.get("/api/tools/add_to_cart").status_code == 404                      # two packs have it
    kiosk = client.get("/api/tools?packs=cafe,common").json()["tools"]                  # what one agent's names mean
    assert sorted(x["id"] for x in kiosk) == sorted(["cafe." + n for n in CAFE] + ["common.handoff_to_human", "common.calculate"])
    drive = {x["name"]: x for x in client.get("/api/tools?packs=burger").json()["tools"]}
    assert drive["add_to_cart"]["pack"] == "burger" and drive["add_to_cart"]["display"] is None
    assert client.get("/api/tools/cafe.get_cart").json()["display"]["kind"] == "cart"
    assert all("display" in t for t in body["tools"])
    assert by_name["get_menu"]["display"] == {"kind": "menu", "path": "", "refresh": []}
    assert by_name["add_to_cart"]["display"] == {"kind": "cart", "path": "cart", "refresh": ["get_cart"]}
    assert by_name["add_to_cart"]["needs_confirmation"] is False
    assert by_name["request_payment"]["display"]["kind"] == "receipt" and by_name["request_payment"]["needs_confirmation"] is True
    assert by_name["get_order"]["display"] is None
    assert client.get("/api/tools/cafe.get_cart").json()["display"]["kind"] == "cart"
    refund = next(t for t in body["tools"] if t["name"] == "request_refund")
    assert refund["effect"] == "write" and refund["needs_confirmation"] is True
    assert refund["confirm_template"] and refund["requires"] == ["verified"] and refund["adapter"]["kind"] == "dummy"
    assert next(t for t in body["tools"] if t["name"] == "handoff_to_human")["needs_confirmation"] is False
    assert client.get("/api/tools/get_weather").json()["filler"] == "날씨 확인해 볼게요."
    assert client.get("/api/tools/nope").status_code == 404


def test_api_tools_reports_a_broken_contract_instead_of_failing(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agentkit.tools.api import router
    (tmp_path / "broken.yaml").write_text("name: broken\n", encoding="utf-8")
    monkeypatch.setattr(config, "TOOLS_DIR", tmp_path)
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/api/tools")
    assert response.status_code == 200
    assert response.json()["tools"] == [] and any("broken.yaml" in e for e in response.json()["errors"])


def test_api_shop_records():
    import asyncio
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agentkit.dummy.shop import get_shop
    from agentkit.tools.api import router
    shop = get_shop()
    shop.reset()
    try:
        asyncio.run(shop.call("handoff_to_human", {"reason": "고객 요청"}, {"session_id": "api-1"}))
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        records = client.get("/api/dummy/shop/records", params={"session_id": "api-1"}).json()["records"]
        assert [r["kind"] for r in records] == ["handoff"] and records[0]["reason"] == "고객 요청"
        assert client.get("/api/dummy/shop/records", params={"kind": "refund"}).json()["records"] == []
    finally:
        shop.reset()


def test_api_cafe_records():
    import asyncio
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agentkit.dummy.cafe import get_cafe
    from agentkit.tools.api import router
    cafe = get_cafe()
    cafe.reset()
    try:
        async def order():
            await cafe.call("add_to_cart", {"item_id": "cheesecake"}, {"session_id": "api-1"})
            await cafe.call("request_payment", {}, {"session_id": "api-1"})
        asyncio.run(order())
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        records = client.get("/api/dummy/cafe/records", params={"session_id": "api-1"}).json()["records"]
        assert [(r["order_no"], r["total"]) for r in records] == [("K-0001", 6500)]
        assert client.get("/api/dummy/cafe/records", params={"session_id": "other"}).json()["records"] == []
    finally:
        cafe.reset()


# ---- packs -------------------------------------------------------------------------------------


def test_packs_let_two_systems_use_the_same_tool_name(tmp_path):
    from agentkit.tools import UnknownTool, resolve
    sample = yaml.safe_load((config.TOOLS_DIR / "burger" / "get_cart.yaml").read_text(encoding="utf-8"))
    for where in ("cafe/find.yaml", "burger/find.yaml", "burger/only.yaml", "alone.yaml"):
        path = tmp_path / where
        path.parent.mkdir(exist_ok=True)
        path.write_text(yaml.safe_dump(dict(sample, name=path.stem, adapter={"kind": "dummy", "target": "cafe:get_cart"}),
                                       allow_unicode=True), encoding="utf-8")
    contracts = load_contracts(tmp_path)
    assert sorted(contracts) == ["alone", "burger.find", "burger.only", "cafe.find"]
    assert (contracts["cafe.find"].pack, contracts["cafe.find"].name, contracts["alone"].pack) == ("cafe", "find", "")
    assert resolve(contracts, "find", ["burger"]) == "burger.find" and resolve(contracts, "find", ["cafe", "x"]) == "cafe.find"
    assert resolve(contracts, "cafe.find", ["burger"]) == "cafe.find"                   # an id says it all
    assert resolve(contracts, "only") == "burger.only" and resolve(contracts, "alone", ["cafe"]) == "alone"
    for name, packs, said in (("find", None, "여러 묶음"), ("find", ["cafe", "burger"], "여러 묶음"),
                              ("only", ["cafe"], "없습니다"), ("nothing", None, "없습니다")):
        with pytest.raises(UnknownTool) as error:
            resolve(contracts, name, packs)
        assert said in str(error.value)
    assert sorted(catalog(contracts, ["burger"])) == ["alone", "find", "only"]
    assert sorted(catalog(contracts)) == ["alone", "burger.find", "cafe.find", "only"]
    gateway = build_gateway(tools_dir=tmp_path)
    assert gateway.names() == sorted(contracts) and gateway.contract("only").id == "burger.only"
    assert [spec["function"]["name"] for spec in gateway.specs(["cafe.find", "burger.only"])] == ["find", "only"]


def test_a_pack_folder_needs_a_plain_name(tmp_path):
    (tmp_path / "Bad-Pack").mkdir()
    with pytest.raises(ContractError) as error:
        load_contracts(tmp_path)
    assert "Bad-Pack" in str(error.value) and "묶음 이름" in str(error.value)
