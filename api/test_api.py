"""Проверка всех эндпоинтов МедЛинк API.  Запуск: pytest -q"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from main import app, now, store

client = TestClient(app)
P = {"Authorization": "Bearer patient-token"}
P2 = {"Authorization": "Bearer patient2-token"}
D = {"Authorization": "Bearer doctor-token"}
A = {"Authorization": "Bearer admin-token"}
WH = {"X-Webhook-Signature": "test-signature"}
MIS = {"X-Internal-Token": "mis-stub-token"}


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()


def first_free_slot(doctor_id=1):
    s = sorted((s for s in store.slots.values()
                if s["doctorId"] == doctor_id and s["isAvailable"] and s["startAt"] - now() > timedelta(hours=5)),
               key=lambda s: s["startAt"])
    return s[0], s[1]


def mis(appt_id, result="confirmed", revision=None):
    if revision is None:
        revision = store.appointments[appt_id].get("bookingRevision", 1)
    return client.post("/api/v1/integrations/mis/booking-result",
                       json={"appointmentId": appt_id, "bookingRevision": revision,
                             "result": result, "misAppointmentId": f"MIS-{appt_id}"},
                       headers=MIS)


def pay_ok(appt, headers=P, confirm=True):
    """Оплата прошла; если confirm, МИС тоже подтвердила запись."""
    pid = client.post(f"/api/v1/appointments/{appt['id']}/payment", headers=headers).json()["paymentId"]
    client.post("/api/v1/payments/webhook", json={"paymentId": pid, "status": "succeeded"}, headers=WH)
    if confirm:
        assert mis(appt["id"]).status_code == 200
    return pid


def book(headers=P, doctor_id=1):
    slot, _ = first_free_slot(doctor_id)
    r = client.post("/api/v1/appointments", json={"slotId": slot["id"], "reason": "Кашель"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


# каталог
def test_list_doctors_and_filters():
    r = client.get("/api/v1/doctors")
    assert r.status_code == 200 and len(r.json()) == 4
    r = client.get("/api/v1/doctors", params={"specialty": "терапевт", "clinicId": 2})
    assert [d["fullName"] for d in r.json()] == ["Кузнецов Дмитрий Павлович"]


def test_doctors_near_me():
    # пациент у площади Гагарина: ближе клиника на Ленинском
    r = client.get("/api/v1/doctors", params={"lat": 55.7070, "lon": 37.5860})
    docs = r.json()
    assert r.status_code == 200 and docs[0]["clinic"]["id"] == 2 and docs[0]["distanceKm"] is not None
    assert [d["distanceKm"] for d in docs] == sorted(d["distanceKm"] for d in docs)
    r = client.get("/api/v1/doctors", params={"lat": 55.7070, "lon": 37.5860, "radiusKm": 3})
    assert {d["clinic"]["id"] for d in r.json()} == {2}
    assert client.get("/api/v1/doctors", params={"lat": 55.7}).status_code == 400


def test_slots():
    slot, _ = first_free_slot()
    r = client.get("/api/v1/doctors/1/slots", params={"date": slot["startAt"].date().isoformat()})
    assert r.status_code == 200 and any(s["id"] == slot["id"] for s in r.json())
    assert client.get("/api/v1/doctors/99/slots", params={"date": "2030-01-01"}).status_code == 404


# сквозной сценарий US 2
def test_book_pay_confirm():
    appt = book()
    assert appt["status"] == "RESERVED" and appt["holdExpiresAt"]
    # слот стал занят, второй пациент его не возьмет
    r = client.post("/api/v1/appointments", json={"slotId": appt["slotId"]}, headers=P2)
    assert r.status_code == 409 and r.json()["code"] == "SLOT_UNAVAILABLE"
    pay = client.post(f"/api/v1/appointments/{appt['id']}/payment", headers=P)
    assert pay.status_code == 201 and pay.json()["status"] == "PENDING"
    pid = pay.json()["paymentId"]
    # вебхук с неверной подписью
    assert client.post("/api/v1/payments/webhook", json={"paymentId": pid, "status": "succeeded"},
                       headers={"X-Webhook-Signature": "bad"}).status_code == 401
    # успешный вебхук, затем повторная доставка того же события
    for _ in range(2):
        assert client.post("/api/v1/payments/webhook", json={"paymentId": pid, "status": "succeeded"},
                           headers=WH).status_code == 200
    r = client.get(f"/api/v1/appointments/{appt['id']}", headers=P).json()
    assert r["status"] == "PAID" and r["payment"]["status"] == "SUCCEEDED"   # оплачено, ждет клинику
    # МИС подтвердила запись; повтор ответа ничего не меняет; чужой токен не принимается
    assert client.post("/api/v1/integrations/mis/booking-result",
                       json={"appointmentId": appt["id"], "bookingRevision": 1, "result": "confirmed"},
                       headers={"X-Internal-Token": "bad"}).status_code == 401
    for _ in range(2):
        assert mis(appt["id"]).status_code == 200
    r = client.get(f"/api/v1/appointments/{appt['id']}", headers=P).json()
    assert r["status"] == "CONFIRMED" and r["misAppointmentId"] == f"MIS-{appt['id']}"


def test_mis_rejects_paid_booking_full_refund():
    # UC 4: оплата прошла, а время в клинике уже занято
    appt = book()
    pay_ok(appt, confirm=False)
    assert mis(appt["id"], "rejected").status_code == 200
    r = client.get(f"/api/v1/appointments/{appt['id']}", headers=P).json()
    assert r["status"] == "CANCELLED" and r["cancelledBy"] == "SYSTEM"
    assert r["payment"]["status"] == "REFUND_PENDING" and r["payment"]["refundAmount"] == r["price"]
    assert not store.slots[appt["slotId"]]["isAvailable"]       # слот занят в МИС, в продажу не возвращается
    # поздний ответ МИС по уже отмененной записи игнорируется
    mis(appt["id"], "confirmed")
    assert store.appointments[appt["id"]]["status"] == "CANCELLED"


def test_reschedule_waits_for_clinic_confirmation():
    appt = book()
    pay_ok(appt, confirm=False)
    _, other = first_free_slot()
    r = client.patch(f"/api/v1/appointments/{appt['id']}", json={"slotId": other["id"]}, headers=P)
    assert r.status_code == 409 and r.json()["code"] == "AWAITING_CLINIC_CONFIRMATION"
    mis(appt["id"])
    r = client.patch(f"/api/v1/appointments/{appt['id']}", json={"slotId": other["id"]}, headers=P)
    assert r.status_code == 200 and r.json()["status"] == "PAID"     # новое время снова ждет клинику
    assert r.json()["payment"]["status"] == "SUCCEEDED"


@pytest.mark.parametrize("old_result", ["confirmed", "rejected"])
def test_old_mis_result_cannot_decide_rescheduled_booking(old_result):
    appt = book()
    pay_ok(appt)
    _, other = first_free_slot()
    changed = client.patch(f"/api/v1/appointments/{appt['id']}",
                           json={"slotId": other["id"]}, headers=P)
    assert changed.status_code == 200
    assert mis(appt["id"], old_result, revision=1).status_code == 200
    current = client.get(f"/api/v1/appointments/{appt['id']}", headers=P).json()
    assert current["status"] == "PAID"
    assert current["payment"]["status"] == "SUCCEEDED"
    assert current["bookingRevision"] == 2
    assert mis(appt["id"], revision=2).status_code == 200
    assert client.get(f"/api/v1/appointments/{appt['id']}", headers=P).json()["status"] == "CONFIRMED"


def test_duplicate_failed_payment_does_not_fail_retry():
    appt = book()
    path = f"/api/v1/appointments/{appt['id']}/payment"
    old_pid = client.post(path, headers=P).json()["paymentId"]
    failed = {"paymentId": old_pid, "status": "failed"}
    assert client.post("/api/v1/payments/webhook", json=failed, headers=WH).status_code == 200
    new_pid = client.post(path, headers=P).json()["paymentId"]
    assert new_pid != old_pid
    assert client.post("/api/v1/payments/webhook", json=failed, headers=WH).status_code == 200
    current = client.get(f"/api/v1/appointments/{appt['id']}", headers=P).json()
    assert current["payment"]["status"] == "PENDING"
    assert current["payment"]["paymentId"] == new_pid
    assert client.post("/api/v1/payments/webhook", json={"paymentId": new_pid, "status": "succeeded"},
                       headers=WH).status_code == 200
    assert client.get(f"/api/v1/appointments/{appt['id']}", headers=P).json()["status"] == "PAID"

def test_hold_expires_and_late_payment_refunded():
    appt = book()
    pid = client.post(f"/api/v1/appointments/{appt['id']}/payment", headers=P).json()["paymentId"]
    store.appointments[appt["id"]]["holdExpiresAt"] = now() - timedelta(seconds=1)
    r = client.get(f"/api/v1/appointments/{appt['id']}", headers=P).json()
    assert r["status"] == "EXPIRED" and store.slots[appt["slotId"]]["isAvailable"]
    client.post("/api/v1/payments/webhook", json={"paymentId": pid, "status": "succeeded"}, headers=WH)
    assert store.appointments[appt["id"]]["payment"]["status"] == "REFUND_PENDING"


# перенос US 4, отмена US 5 и US 9
def test_reschedule():
    appt = book()
    _, other = first_free_slot()
    r = client.patch(f"/api/v1/appointments/{appt['id']}", json={"slotId": other["id"]}, headers=P)
    assert r.status_code == 200 and r.json()["slotId"] == other["id"]
    assert store.slots[appt["slotId"]]["isAvailable"]
    # слот другого врача
    foreign, _ = first_free_slot(doctor_id=2)
    r = client.patch(f"/api/v1/appointments/{appt['id']}", json={"slotId": foreign["id"]}, headers=P)
    assert r.status_code == 400 and r.json()["code"] == "WRONG_DOCTOR"


def test_cancel_early_full_refund():
    appt = book()
    pid = pay_ok(appt)
    r = client.delete(f"/api/v1/appointments/{appt['id']}", headers=P)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "CANCELLED" and body["cancelledBy"] == "PATIENT"
    assert body["payment"]["status"] == "REFUND_PENDING" and body["payment"]["refundAmount"] == body["price"]
    # шлюз подтвердил возврат, повтор вебхука ничего не меняет
    for _ in range(2):
        client.post("/api/v1/payments/webhook", json={"paymentId": pid, "status": "refunded"}, headers=WH)
    assert store.appointments[appt["id"]]["payment"]["status"] == "REFUNDED"


def test_late_cancel_no_refund_and_no_reschedule():
    appt = book()
    pay_ok(appt)
    store.appointments[appt["id"]]["startAt"] = now() + timedelta(hours=1)
    _, other = first_free_slot()
    r = client.patch(f"/api/v1/appointments/{appt['id']}", json={"slotId": other["id"]}, headers=P)
    assert r.status_code == 409 and r.json()["code"] == "TOO_LATE_TO_CHANGE"
    r = client.delete(f"/api/v1/appointments/{appt['id']}", headers=P)
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert r.json()["payment"]["status"] == "SUCCEEDED" and r.json()["payment"]["refundAmount"] == 0


def test_clinic_cancel_always_full_refund():
    appt = book()
    pay_ok(appt)
    store.appointments[appt["id"]]["startAt"] = now() + timedelta(hours=1)
    assert client.delete(f"/api/v1/appointments/{appt['id']}", headers=A).status_code == 422
    r = client.delete(f"/api/v1/appointments/{appt['id']}", params={"reason": "Врач заболел"}, headers=A)
    body = r.json()
    assert r.status_code == 200 and body["cancelledBy"] == "CLINIC" and body["cancelReason"] == "Врач заболел"
    assert body["payment"]["status"] == "REFUND_PENDING" and body["payment"]["refundAmount"] == body["price"]
    # врач отменять записи не может
    other = book(doctor_id=2)
    assert client.delete(f"/api/v1/appointments/{other['id']}", headers=D).status_code == 403


# доступы
def test_auth_and_ownership():
    appt = book()
    assert client.get(f"/api/v1/appointments/{appt['id']}").status_code == 401
    assert client.get(f"/api/v1/appointments/{appt['id']}",
                      headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.delete(f"/api/v1/appointments/{appt['id']}", headers=P2).status_code == 403
    assert client.get(f"/api/v1/appointments/{appt['id']}", headers=D).status_code == 200
    assert client.post("/api/v1/appointments", json={"slotId": 101}, headers=D).status_code == 403


# история US 6
def test_my_appointments_with_record():
    r = client.get("/api/v1/patients/me/appointments", headers=P)
    assert r.status_code == 200
    done = [a for a in r.json() if a["status"] == "COMPLETED"]
    assert done and done[0]["record"]["conclusion"]


# клиника US 7, US 8
def test_clinic_day_and_attendance():
    yesterday = (now() - timedelta(days=1)).date().isoformat()
    r = client.get("/api/v1/clinics/1/appointments", params={"date": yesterday}, headers=A)
    assert r.status_code == 200 and len(r.json()) == 2
    assert all(x["reason"] is None for x in r.json())          # администратор не видит причину обращения
    r = client.get("/api/v1/clinics/1/appointments", params={"date": yesterday}, headers=D)
    assert r.status_code == 200 and all(x["doctor"]["id"] == 1 for x in r.json())
    assert all(x["reason"] for x in r.json())                   # врач видит
    assert client.get("/api/v1/clinics/2/appointments", params={"date": yesterday},
                      headers=A).status_code == 403
    r = client.patch("/api/v1/appointments/1002/attendance", json={"attendance": "NO_SHOW"}, headers=A)
    assert r.status_code == 200 and r.json()["status"] == "NO_SHOW"
    # будущую запись отметить нельзя
    appt = book()
    pay_ok(appt)
    r = client.patch(f"/api/v1/appointments/{appt['id']}/attendance", json={"attendance": "ATTENDED"}, headers=A)
    assert r.status_code == 409 and r.json()["code"] == "NOT_STARTED"


def test_doctor_sees_records_only_with_active_appointment():
    # у пациента 1 нет будущей записи к врачу 1 - доступа нет
    r = client.get("/api/v1/patients/1/records", headers=D)
    assert r.status_code == 403 and r.json()["code"] == "NO_ACTIVE_APPOINTMENT"
    pay_ok(book())
    r = client.get("/api/v1/patients/1/records", headers=D)
    assert r.status_code == 200 and r.json()[0]["conclusion"]
    assert client.get("/api/v1/patients/1/records", headers=A).status_code == 403
    assert client.get("/api/v1/patients/1/records", headers=P).status_code == 403
    assert client.get("/api/v1/patients/99/records", headers=D).status_code == 404


# ролевая модель: медданные скрыты от администратора
def test_admin_never_gets_medical_data():
    # 1001 - состоявшийся прием пациента 1 с причиной обращения и заключением
    for headers in (P, D):
        r = client.get("/api/v1/appointments/1001", headers=headers).json()
        assert r["reason"] and r["record"]["conclusion"]
    r = client.get("/api/v1/appointments/1001", headers=A)
    assert r.status_code == 200 and r.json()["reason"] is None and r.json()["record"] is None
    r = client.patch("/api/v1/appointments/1002/attendance", json={"attendance": "ATTENDED"}, headers=A)
    assert r.status_code == 200 and r.json()["reason"] is None and r.json()["record"] is None
    appt = book()
    pay_ok(appt)
    r = client.delete(f"/api/v1/appointments/{appt['id']}", params={"reason": "Врач заболел"}, headers=A)
    assert r.status_code == 200 and r.json()["reason"] is None and r.json()["record"] is None
    # пациент по-прежнему видит свою причину обращения
    assert client.get(f"/api/v1/appointments/{appt['id']}", headers=P).json()["reason"] == "Кашель"
