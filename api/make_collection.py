"""Генерирует Postman-коллекцию medlink.postman_collection.json (сквозной сценарий по порядку)."""
import json
from pathlib import Path


def req(name, method, path, token=None, body=None, query=None, tests=None, headers=None, description=""):
    h = []
    if token:
        h.append({"key": "Authorization", "value": "Bearer {{" + token + "}}"})
    if body is not None:
        h.append({"key": "Content-Type", "value": "application/json"})
    for k, v in (headers or {}).items():
        h.append({"key": k, "value": v})
    raw = "{{baseUrl}}" + path + (("?" + "&".join(f"{k}={v}" for k, v in query.items())) if query else "")
    item = {"name": name, "request": {"method": method, "header": h, "url": {
        "raw": raw, "host": ["{{baseUrl}}"], "path": [p for p in path.strip("/").split("/")],
        **({"query": [{"key": k, "value": v} for k, v in query.items()]} if query else {})},
        "description": description}}
    if body is not None:
        raw_body = json.dumps(body, ensure_ascii=False, indent=2)
        for var in ("slotId", "slotId2", "slotId3", "appointmentId", "bookingRevision"):  # числовые переменные
            raw_body = raw_body.replace('"{{' + var + '}}"', '{{' + var + '}}')
        item["request"]["body"] = {"mode": "raw", "raw": raw_body}
    if tests:
        item["event"] = [{"listen": "test", "script": {"type": "text/javascript", "exec": tests}}]
    return item


def status_test(code):
    return [f'pm.test("Статус {code}", () => pm.response.to.have.status({code}));']


collection_prerequest = [
    "// Даты в часовом поясе МСК: завтра - для записи, вчера - для отметки неявки",
    "const msk = new Date(Date.now() + 3 * 3600 * 1000);",
    "pm.collectionVariables.set('tomorrow', new Date(msk.getTime() + 86400000).toISOString().slice(0, 10));",
    "pm.collectionVariables.set('yesterday', new Date(msk.getTime() - 86400000).toISOString().slice(0, 10));",
]

MIS_H = {"X-Internal-Token": "{{misToken}}"}

items = [
    req("1. Найти врачей-терапевтов", "GET", "/api/v1/doctors", query={"specialty": "терапевт"},
        tests=status_test(200) + ['pm.test("Есть врачи", () => pm.expect(pm.response.json().length).to.be.above(0));'],
        description="US 1. Публичный метод, токен не нужен."),
    req("2. Врачи рядом со мной (до 3 км)", "GET", "/api/v1/doctors",
        query={"lat": "55.7070", "lon": "37.5860", "radiusKm": "3"},
        tests=status_test(200) + [
            "const d = pm.response.json();",
            "pm.test('Только ближняя клиника, есть расстояние', () => { pm.expect(d.length).to.be.above(0); "
            "d.forEach(x => pm.expect(x.distanceKm).to.be.below(3)); });"],
        description="US 1. Координаты пациента у площади Гагарина: из геолокации или из введенного адреса."),
    req("3. Расписание врача на завтра", "GET", "/api/v1/doctors/1/slots", query={"date": "{{tomorrow}}"},
        tests=status_test(200) + [
            "const free = pm.response.json().filter(s => s.isAvailable);",
            "pm.test('Есть свободные слоты', () => pm.expect(free.length).to.be.above(2));",
            "pm.collectionVariables.set('slotId', free[0].id);",
            "pm.collectionVariables.set('slotId2', free[1].id);",
            "pm.collectionVariables.set('slotId3', free[2].id);"],
        description="US 1. Сохраняет три свободных слота в переменные коллекции."),
    req("4. Записаться на прием", "POST", "/api/v1/appointments", token="patientToken",
        body={"slotId": "{{slotId}}", "reason": "Температура и кашель третий день"},
        tests=status_test(201) + [
            "const a = pm.response.json();",
            "pm.test('Статус RESERVED', () => pm.expect(a.status).to.eql('RESERVED'));",
            "pm.collectionVariables.set('appointmentId', a.id);",
            "pm.collectionVariables.set('bookingRevision', a.bookingRevision);"],
        description="US 2. Слот удерживается 10 минут до оплаты."),
    req("5. Получить запись", "GET", "/api/v1/appointments/{{appointmentId}}", token="patientToken",
        tests=status_test(200), description="US 2."),
    req("6. Создать платеж", "POST", "/api/v1/appointments/{{appointmentId}}/payment", token="patientToken",
        tests=status_test(201) + ["pm.collectionVariables.set('paymentId', pm.response.json().paymentId);"],
        description="US 2. Возвращает ссылку на платежную форму шлюза."),
    req("7. Вебхук: оплата прошла", "POST", "/api/v1/payments/webhook",
        body={"paymentId": "{{paymentId}}", "status": "succeeded"},
        headers={"X-Webhook-Signature": "{{webhookSignature}}"},
        tests=status_test(200), description="US 2. Этот запрос в жизни отправляет платежный шлюз."),
    req("8. Проверить статус «Оплачено, ожидает подтверждения клиникой»", "GET",
        "/api/v1/appointments/{{appointmentId}}", token="patientToken",
        tests=status_test(200) + ["pm.test('Статус PAID', () => pm.expect(pm.response.json().status).to.eql('PAID'));"],
        description="US 2. Оплата прошла, запись передана в МИС и ждет ответа."),
    req("9. Ответ МИС: запись принята", "POST", "/api/v1/integrations/mis/booking-result",
        body={"appointmentId": "{{appointmentId}}", "bookingRevision": "{{bookingRevision}}",
              "result": "confirmed", "misAppointmentId": "MIS-0001"},
        headers=MIS_H, tests=status_test(200),
        description="US 2. В жизни это событие integration-service, в прототипе служебный метод."),
    req("10. Проверить, что запись подтверждена", "GET", "/api/v1/appointments/{{appointmentId}}",
        token="patientToken",
        tests=status_test(200) + ["pm.test('Статус CONFIRMED', () => pm.expect(pm.response.json().status).to.eql('CONFIRMED'));"]),
    req("11. Перенести запись", "PATCH", "/api/v1/appointments/{{appointmentId}}", token="patientToken",
        body={"slotId": "{{slotId2}}"},
        tests=status_test(200) + [
            "const a = pm.response.json();",
            "pm.collectionVariables.set('bookingRevision', a.bookingRevision);",
            "pm.test('Слот изменился, оплата сохранилась, новое время ждет клинику', () => { "
            "pm.expect(a.slotId).to.eql(pm.collectionVariables.get('slotId2') * 1); "
            "pm.expect(a.payment.status).to.eql('SUCCEEDED'); pm.expect(a.status).to.eql('PAID'); });"],
        description="US 4. Перенос без доплаты и без возврата."),
    req("12. Ответ МИС: новое время принято", "POST", "/api/v1/integrations/mis/booking-result",
        body={"appointmentId": "{{appointmentId}}", "bookingRevision": "{{bookingRevision}}",
              "result": "confirmed", "misAppointmentId": "MIS-0002"},
        headers=MIS_H, tests=status_test(200), description="US 4."),
    req("13. Мои записи и заключения", "GET", "/api/v1/patients/me/appointments", token="patientToken",
        tests=status_test(200), description="US 6."),
    req("14. Врач смотрит прошлые заключения пациента", "GET", "/api/v1/patients/1/records", token="doctorToken",
        tests=status_test(200) + ["pm.test('Есть заключение', () => pm.expect(pm.response.json()[0].conclusion).to.be.a('string'));"],
        description="US 7. Доступ есть, потому что у пациента есть подтвержденная запись к этому врачу."),
    req("15. Ошибка: администратор не видит заключения", "GET", "/api/v1/patients/1/records", token="adminToken",
        tests=status_test(403), description="Ролевая модель: заключения видит только врач."),
    req("16. Администратор открывает запись без медданных", "GET", "/api/v1/appointments/1001", token="adminToken",
        tests=status_test(200) + [
            "const a = pm.response.json();",
            "pm.test('Причина и заключение скрыты', () => { pm.expect(a.reason).to.eql(null); pm.expect(a.record).to.eql(null); });"],
        description="Ролевая модель: у записи 1001 есть причина обращения и заключение, но администратор их не получает."),
    req("17. Записи клиники за вчера (администратор)", "GET", "/api/v1/clinics/1/appointments",
        token="adminToken", query={"date": "{{yesterday}}"},
        tests=status_test(200) + ["pm.test('Причина обращения скрыта', () => pm.response.json().forEach(x => pm.expect(x.reason).to.eql(null)));"],
        description="US 8."),
    req("18. Отметить неявку (администратор)", "PATCH", "/api/v1/appointments/1002/attendance",
        token="adminToken", body={"attendance": "NO_SHOW"},
        tests=status_test(200) + [
            "const a = pm.response.json();",
            "pm.test('Статус NO_SHOW, медданных нет', () => { pm.expect(a.status).to.eql('NO_SHOW'); pm.expect(a.reason).to.eql(null); });"],
        description="US 8. Запись 1002 создается при запуске сервера. После отметки повторный прогон вернет 409 - перезапустите сервер."),
    req("19. Отменить запись заранее: возврат 100%", "DELETE", "/api/v1/appointments/{{appointmentId}}",
        token="patientToken",
        tests=status_test(200) + [
            "const a = pm.response.json();",
            "pm.test('Отменена, запущен полный возврат', () => { pm.expect(a.status).to.eql('CANCELLED'); "
            "pm.expect(a.payment.status).to.eql('REFUND_PENDING'); pm.expect(a.payment.refundAmount).to.eql(a.price); });"],
        description="US 5. До приема больше 4 часов."),
    req("20. Вебхук: возврат завершен", "POST", "/api/v1/payments/webhook",
        body={"paymentId": "{{paymentId}}", "status": "refunded"},
        headers={"X-Webhook-Signature": "{{webhookSignature}}"},
        tests=status_test(200), description="US 5."),
    req("21. Проверить статус REFUNDED", "GET", "/api/v1/appointments/{{appointmentId}}", token="patientToken",
        tests=status_test(200) + ["pm.test('Деньги вернулись', () => pm.expect(pm.response.json().payment.status).to.eql('REFUNDED'));"]),
    req("22. Новая запись для отмены клиникой", "POST", "/api/v1/appointments", token="patientToken",
        body={"slotId": "{{slotId3}}", "reason": "Плановый осмотр"},
        tests=status_test(201) + ["pm.collectionVariables.set('appointmentId2', pm.response.json().id);"],
        description="US 9, подготовка."),
    req("23. Оплатить новую запись", "POST", "/api/v1/appointments/{{appointmentId2}}/payment", token="patientToken",
        tests=status_test(201) + ["pm.collectionVariables.set('paymentId2', pm.response.json().paymentId);"]),
    req("24. Вебхук: оплата новой записи", "POST", "/api/v1/payments/webhook",
        body={"paymentId": "{{paymentId2}}", "status": "succeeded"},
        headers={"X-Webhook-Signature": "{{webhookSignature}}"}, tests=status_test(200)),
    req("25. Отмена по инициативе клиники", "DELETE", "/api/v1/appointments/{{appointmentId2}}",
        token="adminToken", query={"reason": "Врач заболел"},
        tests=status_test(200) + [
            "const a = pm.response.json();",
            "pm.test('Отменила клиника, возврат 100%, медданных нет', () => { pm.expect(a.cancelledBy).to.eql('CLINIC'); "
            "pm.expect(a.payment.refundAmount).to.eql(a.price); pm.expect(a.reason).to.eql(null); });"],
        description="US 9. Возврат всей суммы независимо от времени до приема."),
    req("26. Ошибка: запись без токена", "POST", "/api/v1/appointments", body={"slotId": "{{slotId}}"},
        tests=status_test(401), description="Проверка авторизации."),
]

collection = {
    "info": {"name": "МедЛинк API", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
             "description": "Сквозной сценарий: поиск врача рядом, запись, оплата, подтверждение клиникой, перенос, заключения, "
                            "скрытие медданных от администратора, неявка, отмена с возвратом, "
                            "отмена клиникой. "
                            "Запустите сервер (uvicorn main:app), укажите его адрес в переменной baseUrl и прогоните коллекцию через Run collection."},
    "event": [{"listen": "prerequest", "script": {"type": "text/javascript", "exec": collection_prerequest}}],
    "variable": [
        {"key": "baseUrl", "value": "", "description": "Адрес сервера, который uvicorn выводит в консоль при запуске"},
        {"key": "patientToken", "value": "patient-token"},
        {"key": "adminToken", "value": "admin-token"},
        {"key": "doctorToken", "value": "doctor-token"},
        {"key": "webhookSignature", "value": "test-signature"},
        {"key": "misToken", "value": "mis-stub-token"},
        {"key": "tomorrow", "value": ""}, {"key": "yesterday", "value": ""},
        {"key": "slotId", "value": ""}, {"key": "slotId2", "value": ""}, {"key": "slotId3", "value": ""},
        {"key": "appointmentId", "value": ""}, {"key": "paymentId", "value": ""},
        {"key": "bookingRevision", "value": ""},
        {"key": "appointmentId2", "value": ""}, {"key": "paymentId2", "value": ""},
    ],
    "item": items,
}

with Path(__file__).with_name("medlink.postman_collection.json").open("w", encoding="utf-8") as f:
    json.dump(collection, f, ensure_ascii=False, indent=2)
print("requests:", len(items))
