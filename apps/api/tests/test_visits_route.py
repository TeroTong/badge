from smart_badge_api.api.routes.visits import _build_visit_order_context, _to_out
from smart_badge_api.db.models import Visit, VisitOrder


def test_build_visit_order_context_includes_line_items_for_merged_dzdh() -> None:
    orders = [
        VisitOrder(
            dzdh="DZ001",
            dzseg="110",
            fzdh="DZ001-110",
            advxc="81020169",
            advxc_long="谢静",
            fzsj="08:45:15",
            fzsta_txt="已分诊",
            jcsta_txt="已成交",
            kutyp_dq="V",
            kutyp_dq_txt="会员/老客",
            remark_dz="双眼皮咨询",
        ),
        VisitOrder(
            dzdh="DZ001",
            dzseg="120",
            fzdh="DZ001-120",
            advxc="81021091",
            advxc_long="刘玲",
            fzsj="08:58:05",
            fzsta_txt="已分诊",
            jcsta_txt="待跟进",
            remark_dz="眼综合",
        ),
    ]

    context = _build_visit_order_context(orders)

    assert context is not None
    assert len(context.line_items) == 2
    assert [item.fzdh for item in context.line_items] == ["DZ001-110", "DZ001-120"]
    assert context.customer_type_code == "V"
    assert context.customer_type_label == "老客"
    assert context.line_items[0].triage_staff_name == "谢静"
    assert context.line_items[1].consult_project == "眼综合"
    assert context.line_items[1].note_summary == "眼综合"


def test_visit_out_normalizes_customer_type_from_kutyp() -> None:
    visit = Visit(id="visit001", customer_id="cust001", status="consulted")

    payload = _to_out(
        visit,
        customer_name="周琴",
        customer_code="C001",
        customer_source=None,
        recording_count=0,
        customer_type_code="Q",
        customer_type_label="潜客/新客",
    )

    assert payload.customer_type_code == "Q"
    assert payload.customer_type_label == "新客"
