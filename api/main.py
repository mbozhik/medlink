"""
МедЛинк API - учебный прототип серверной части приложения сети медицинских клиник.

Данные хранятся в памяти и пересоздаются при каждом запуске. МИС и платежный
шлюз имитируются служебными методами; доставка уведомлений не реализована.

Запуск:  uvicorn main:app --reload
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from itertools import count
from math import asin, cos, radians, sin, sqrt
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

MSK = timezone(timedelta(hours=3))
HOLD_MINUTES = 10           # резерв слота на время оплаты
CHANGE_DEADLINE_HOURS = 4   # перенос не позже чем за 4 часа, отмена позже - без возврата
WEBHOOK_SECRET = "test-signature"
MIS_TOKEN = "mis-stub-token"         # служебный токен integration-service (заглушка МИС)


def now() -> datetime:
    return datetime.now(MSK).replace(microsecond=0)


# Модели данных (они же схемы OpenAPI)

AppointmentStatus = Literal["RESERVED", "PAID", "CONFIRMED", "CANCELLED", "EXPIRED", "COMPLETED", "NO_SHOW"]
PaymentStatus = Literal["PENDING", "SUCCEEDED", "FAILED", "REFUND_PENDING", "REFUNDED"]
CancelledBy = Literal["PATIENT", "CLINIC", "SYSTEM"]


class ErrorResponse(BaseModel):
    code: str = Field(description="Машиночитаемый код ошибки")
    message: str = Field(description="Текст ошибки для пользователя")


class Clinic(BaseModel):
    id: int = Field(examples=[1])
    name: str = Field(examples=["МедЛинк на Тверской"])
    address: str = Field(examples=["Москва, ул. Тверская, 12"])
    latitude: float = Field(description="Широта клиники", examples=[55.7616])
    longitude: float = Field(description="Долгота клиники", examples=[37.6087])


class DoctorRef(BaseModel):
    id: int = Field(examples=[1])
    fullName: str = Field(examples=["Иванова Анна Сергеевна"])
    specialty: str = Field(examples=["терапевт"])


class Doctor(DoctorRef):
    clinic: Clinic
    price: int = Field(description="Стоимость приема, руб.", examples=[2500])
    rating: float = Field(ge=0, le=5, description="Рейтинг врача", examples=[4.8])
    nearestSlotAt: Optional[datetime] = Field(
        description="Ближайший свободный слот (с учетом фильтра по дате). null, если свободных нет")
    distanceKm: Optional[float] = Field(
        description="Расстояние от пациента до клиники, км. Заполняется, если переданы lat и lon", examples=[1.4])


class Slot(BaseModel):
    id: int = Field(examples=[101])
    doctorId: int = Field(examples=[1])
    startAt: datetime = Field(description="Начало приема (ISO 8601, МСК)")
    endAt: datetime = Field(description="Окончание приема (ISO 8601, МСК)")
    cabinet: str = Field(description="Номер кабинета", examples=["214"])
    isAvailable: bool = Field(description="Свободен ли слот для записи")


class PaymentInfo(BaseModel):
    paymentId: str = Field(examples=["pay_7f3a91"])
    status: PaymentStatus = Field(description="PENDING - ожидает оплаты, SUCCEEDED - оплачено, "
                                              "FAILED - ошибка оплаты, REFUND_PENDING - запущен возврат, "
                                              "REFUNDED - деньги вернулись на карту")
    amount: int = Field(description="Сумма, руб.", examples=[2500])
    refundAmount: Optional[int] = Field(default=None, description="Сумма возврата, руб. 0 - отмена позже "
                                        "чем за 4 часа, деньги не возвращаются. null - отмены не было",
                                        examples=[2500])
    paymentUrl: Optional[str] = Field(description="Ссылка на платежную форму шлюза")


class MedicalRecord(BaseModel):
    id: int = Field(examples=[501])
    appointmentId: int = Field(examples=[1001])
    conclusion: str = Field(description="Краткое заключение врача", examples=["ОРВИ, назначено лечение"])
    fileUrl: str = Field(description="Ссылка на PDF заключения в хранилище")
    issuedAt: datetime = Field(description="Когда заключение получено из МИС")


class Appointment(BaseModel):
    id: int = Field(examples=[1001])
    patientId: int = Field(examples=[1])
    doctor: DoctorRef
    clinic: Clinic = Field(description="Снимок данных клиники на момент записи (адрес попадает в напоминания)")
    slotId: int = Field(examples=[101])
    bookingRevision: int = Field(ge=1, description="Версия записи для корреляции ответа МИС; увеличивается при переносе", examples=[1])
    startAt: datetime
    cabinet: str = Field(examples=["214"])
    reason: Optional[str] = Field(description="Причина обращения со слов пациента. "
                                              "Для администратора всегда null: это медицинские данные")
    price: int = Field(examples=[2500])
    status: AppointmentStatus = Field(
        description="RESERVED - слот удерживается до оплаты; PAID - оплачено, ожидает подтверждения "
                    "клиникой (МИС); CONFIRMED - подтверждено клиникой; CANCELLED - отменено; "
                    "EXPIRED - не оплачено вовремя; COMPLETED - прием состоялся; NO_SHOW - неявка")
    misAppointmentId: Optional[str] = Field(default=None, description="Номер записи в МИС после подтверждения",
                                            examples=["MIS-0001003"])
    holdExpiresAt: Optional[datetime] = Field(description="До какого времени удерживается слот (для RESERVED)")
    payment: Optional[PaymentInfo] = None
    cancelledBy: Optional[CancelledBy] = Field(
        default=None, description="Кто отменил запись: PATIENT - пациент, CLINIC - клиника, "
                                  "SYSTEM - система (конфликт с МИС)")
    cancelReason: Optional[str] = Field(default=None, examples=["Врач заболел"])
    record: Optional[MedicalRecord] = Field(default=None, description="Заключение врача, если прием состоялся. "
                                            "Для администратора всегда null")
    createdAt: datetime
    updatedAt: datetime


class ClinicAppointment(BaseModel):
    id: int = Field(examples=[1001])
    startAt: datetime
    cabinet: str
    doctor: DoctorRef
    patientId: int = Field(examples=[1])
    patientName: str = Field(examples=["Петров Иван"])
    reason: Optional[str] = Field(description="Причина обращения. Видит только врач, для администратора null")
    status: AppointmentStatus
    paymentStatus: Optional[PaymentStatus]


class AppointmentCreate(BaseModel):
    slotId: int = Field(description="Идентификатор свободного слота", examples=[101])
    reason: Optional[str] = Field(default=None, max_length=500, description="Причина обращения",
                                  examples=["Температура и кашель третий день"])


class AppointmentReschedule(BaseModel):
    slotId: int = Field(description="Новый слот того же врача", examples=[102])


class PaymentWebhook(BaseModel):
    paymentId: str = Field(examples=["pay_7f3a91"])
    status: Literal["succeeded", "failed", "refunded"] = Field(
        description="succeeded - оплата прошла, failed - оплата отклонена, refunded - возврат завершен")


class PatientRecord(BaseModel):
    id: int = Field(examples=[501])
    appointmentId: int = Field(examples=[1001])
    visitAt: datetime = Field(description="Дата и время приема")
    doctor: DoctorRef
    conclusion: str = Field(examples=["Здоров, рекомендован повторный осмотр через год"])
    fileUrl: str


class WebhookAck(BaseModel):
    received: bool = True


class MisBookingResult(BaseModel):
    appointmentId: int = Field(examples=[1003])
    bookingRevision: int = Field(ge=1, description="Версия записи, переданная в МИС; устаревшие ответы игнорируются", examples=[1])
    result: Literal["confirmed", "rejected"] = Field(
        description="confirmed - МИС приняла запись, rejected - МИС отклонила (время уже занято в клинике)")
    misAppointmentId: Optional[str] = Field(default=None, description="Номер записи в МИС, для confirmed",
                                            examples=["MIS-0001003"])


class AttendanceUpdate(BaseModel):
    attendance: Literal["ATTENDED", "NO_SHOW"] = Field(description="ATTENDED - пациент пришел, NO_SHOW - неявка")


# Ошибки

class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def err(status: int, description: str, code: Optional[str] = None) -> dict:
    codes = {400: "SLOT_IN_PAST", 401: "UNAUTHORIZED", 403: "FORBIDDEN",
             404: "APPOINTMENT_NOT_FOUND", 409: "INVALID_STATUS", 422: "REASON_REQUIRED"}
    return {status: {"model": ErrorResponse, "description": description,
                     "content": {"application/json": {"example": {
                         "code": code or codes[status], "message": description}}}}}


R401 = err(401, "Нет токена или токен недействителен")
R403 = err(403, "Нет прав на этот ресурс")


# Хранилище в памяти

class Store:
    def __init__(self):
        self.reset()

    def reset(self):
        self.clinics = {
            1: Clinic(id=1, name="МедЛинк на Тверской", address="Москва, ул. Тверская, 12",
                      latitude=55.7616, longitude=37.6087),
            2: Clinic(id=2, name="МедЛинк на Ленинском", address="Москва, Ленинский пр-т, 45",
                      latitude=55.7059, longitude=37.5733),
        }
        self.doctors = {
            1: dict(id=1, fullName="Иванова Анна Сергеевна", specialty="терапевт", clinicId=1, price=2500, rating=4.8),
            2: dict(id=2, fullName="Петров Олег Игоревич", specialty="кардиолог", clinicId=1, price=3500, rating=4.6),
            3: dict(id=3, fullName="Смирнова Мария Алексеевна", specialty="невролог", clinicId=2, price=3200, rating=4.9),
            4: dict(id=4, fullName="Кузнецов Дмитрий Павлович", specialty="терапевт", clinicId=2, price=2300, rating=4.5),
        }
        self.patients = {1: "Петров Иван", 2: "Смирнова Ольга"}
        self.users = {
            "patient-token": dict(role="patient", patientId=1),
            "patient2-token": dict(role="patient", patientId=2),
            "doctor-token": dict(role="doctor", doctorId=1),
            "admin-token": dict(role="admin", clinicId=1),
        }
        self.slots: dict[int, dict] = {}
        self.appointments: dict[int, dict] = {}
        self.payments: dict[str, tuple[int, dict]] = {}  # paymentId -> (appointmentId, конкретная попытка оплаты)
        self._slot_ids = count(101)
        self._appt_ids = count(1001)
        self._pay_ids = count(1)
        self._seed()

    # генерация расписания: 7 дней вперед, 09:00-13:00 каждые 30 минут
    def _seed(self):
        today = now().date()
        cabinets = {1: "214", 2: "305", 3: "112", 4: "118"}
        for day in range(0, 7):
            d = today + timedelta(days=day)
            for doc_id in self.doctors:
                for i in range(8):
                    start = datetime.combine(d, time(9, 0), MSK) + timedelta(minutes=30 * i)
                    if start > now():
                        self._add_slot(doc_id, start, cabinets[doc_id])
        # прошлые приемы: история пациента и пример для отметки неявки
        y = today - timedelta(days=1)
        s1 = self._add_slot(1, datetime.combine(y, time(10, 0), MSK), "214", available=False)
        a1 = self._add_appt(1, s1, "Плановый осмотр", "COMPLETED", paid=True)
        a1["record"] = dict(id=501, appointmentId=a1["id"], conclusion="Здоров, рекомендован повторный осмотр через год",
                            fileUrl="https://s3.medlink.example/records/501.pdf",
                            issuedAt=datetime.combine(y, time(12, 0), MSK))
        s2 = self._add_slot(1, datetime.combine(y, time(11, 0), MSK), "214", available=False)
        self._add_appt(2, s2, "Боль в горле", "CONFIRMED", paid=True)

    def _add_slot(self, doctor_id, start, cabinet, available=True) -> int:
        sid = next(self._slot_ids)
        self.slots[sid] = dict(id=sid, doctorId=doctor_id, startAt=start, endAt=start + timedelta(minutes=30),
                               cabinet=cabinet, isAvailable=available)
        return sid

    def _add_appt(self, patient_id, slot_id, reason, status, paid=False) -> dict:
        slot = self.slots[slot_id]
        doc = self.doctors[slot["doctorId"]]
        aid = next(self._appt_ids)
        payment = None
        if paid:
            pid = f"pay_{next(self._pay_ids):06d}"
            payment = dict(paymentId=pid, status="SUCCEEDED", amount=doc["price"], refundAmount=None, paymentUrl=None)
            self.payments[pid] = (aid, payment)
        appt = dict(id=aid, patientId=patient_id, doctorId=doc["id"], clinicId=doc["clinicId"], slotId=slot_id,
                    startAt=slot["startAt"], cabinet=slot["cabinet"], reason=reason, price=doc["price"],
                    status=status, bookingRevision=1, holdExpiresAt=None, payment=payment, cancelledBy=None, cancelReason=None,
                    record=None, createdAt=now(), updatedAt=now())
        self.appointments[aid] = appt
        return appt

    # снять резервы, которые не оплатили за HOLD_MINUTES (в проде это джоба)
    def expire_holds(self):
        for a in self.appointments.values():
            if a["status"] == "RESERVED" and a["holdExpiresAt"] and a["holdExpiresAt"] <= now():
                a["status"], a["holdExpiresAt"], a["updatedAt"] = "EXPIRED", None, now()
                self.slots[a["slotId"]]["isAvailable"] = True

    # сборка ответов
    def doctor_ref(self, doc_id) -> DoctorRef:
        d = self.doctors[doc_id]
        return DoctorRef(id=d["id"], fullName=d["fullName"], specialty=d["specialty"])

    def appointment_out(self, a, viewer: Optional[dict] = None) -> Appointment:
        # ролевая модель: администратор не видит медицинские данные - причину обращения и заключение
        medical = viewer is None or viewer["role"] != "admin"
        return Appointment(
            id=a["id"], patientId=a["patientId"], doctor=self.doctor_ref(a["doctorId"]),
            clinic=self.clinics[a["clinicId"]], slotId=a["slotId"], bookingRevision=a["bookingRevision"],
            startAt=a["startAt"], cabinet=a["cabinet"],
            reason=a["reason"] if medical else None, price=a["price"], status=a["status"],
            misAppointmentId=a.get("misAppointmentId"), holdExpiresAt=a["holdExpiresAt"],
            payment=PaymentInfo(**a["payment"]) if a["payment"] else None,
            cancelledBy=a["cancelledBy"], cancelReason=a["cancelReason"],
            record=MedicalRecord(**a["record"]) if a["record"] and medical else None,
            createdAt=a["createdAt"], updatedAt=a["updatedAt"])


store = Store()


# Авторизация (заглушка)

bearer = HTTPBearer(auto_error=False, description="Тестовые токены: patient-token, patient2-token, doctor-token, admin-token")


async def current_user(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> dict:
    if creds is None:
        raise ApiError(401, "UNAUTHORIZED", "Требуется авторизация")
    user = store.users.get(creds.credentials)
    if user is None:
        raise ApiError(401, "INVALID_TOKEN", "Токен недействителен")
    return user


async def patient_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "patient":
        raise ApiError(403, "FORBIDDEN", "Действие доступно только пациенту")
    return user


def get_appointment(appointment_id: int) -> dict:
    store.expire_holds()
    a = store.appointments.get(appointment_id)
    if a is None:
        raise ApiError(404, "APPOINTMENT_NOT_FOUND", "Запись не найдена")
    return a


def own_appointment(appointment_id: int, user: dict) -> dict:
    a = get_appointment(appointment_id)
    if a["patientId"] != user["patientId"]:
        raise ApiError(403, "FORBIDDEN", "Это запись другого пациента")
    return a


def check_active(a: dict):
    if a["status"] not in ("RESERVED", "PAID", "CONFIRMED"):
        raise ApiError(409, "INVALID_STATUS", f"Запись в статусе {a['status']} нельзя изменить")
    if a["startAt"] <= now():
        raise ApiError(409, "ALREADY_STARTED", "Прием уже начался")


def check_change_deadline(a: dict):
    check_active(a)
    if a["startAt"] - now() < timedelta(hours=CHANGE_DEADLINE_HOURS):
        raise ApiError(409, "TOO_LATE_TO_CHANGE",
                       f"Перенос возможен не позднее чем за {CHANGE_DEADLINE_HOURS} часа до приема")


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние по прямой (формула гаверсинусов)."""
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    h = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return round(2 * 6371 * asin(sqrt(h)), 1)


def take_slot(slot_id: int, doctor_id: Optional[int] = None) -> dict:
    slot = store.slots.get(slot_id)
    if slot is None:
        raise ApiError(404, "SLOT_NOT_FOUND", "Слот не найден")
    if doctor_id is not None and slot["doctorId"] != doctor_id:
        raise ApiError(400, "WRONG_DOCTOR", "Перенос возможен только на слот того же врача")
    if slot["startAt"] <= now():
        raise ApiError(400, "SLOT_IN_PAST", "Слот уже прошел")
    if not slot["isAvailable"]:
        raise ApiError(409, "SLOT_UNAVAILABLE", "Слот уже занят")
    slot["isAvailable"] = False
    return slot


# Приложение

app = FastAPI(
    title="МедЛинк API",
    version="1.0.0",
    description=(
        "Серверная часть мобильного приложения сети медицинских клиник «МедЛинк»: поиск врача, "
        "запись с онлайн-оплатой, перенос и отмена, история визитов, рабочие места врача и администратора.\n\n"
        "**Авторизация:** заголовок `Authorization: Bearer <токен>` с одним из тестовых токенов:\n"
        "* `patient-token` - пациент Петров Иван;\n"
        "* `patient2-token` - пациентка Смирнова Ольга;\n"
        "* `doctor-token` - врач Иванова А. С. (клиника 1);\n"
        "* `admin-token` - администратор клиники 1.\n\n"
        "Служебный метод для ответа МИС вызывается с заголовком `X-Internal-Token: mis-stub-token`.\n\n"
        "Данные хранятся в памяти и сбрасываются при перезапуске. МИС и платежный шлюз имитируются "
        "служебными методами; доставка уведомлений не реализована."
    ),
    openapi_tags=[
        {"name": "Каталог", "description": "Врачи и свободные слоты (US 1)"},
        {"name": "Записи", "description": "Запись, перенос, отмена (US 2, US 4, US 5, US 9)"},
        {"name": "Оплата", "description": "Оплата записи, возврат и вебхук платежного шлюза (US 2, US 5)"},
        {"name": "Пациент", "description": "История визитов и заключения (US 6, US 7)"},
        {"name": "Клиника", "description": "Рабочие места врача и администратора (US 7, US 8, US 9)"},
        {"name": "Интеграция", "description": "Ответ МИС на передачу записи (US 2, UC 4). В целевой архитектуре "
                                              "это событие integration-service, в прототипе - служебный метод"},
    ],
)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": exc.message})


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/docs")


# 1. Каталог врачей
@app.get("/api/v1/doctors", tags=["Каталог"], response_model=list[Doctor],
         summary="Найти врачей",
         description="Возвращает врачей с ближайшим свободным слотом. Фильтры необязательные и сочетаются. "
                     "Для фильтра «рядом со мной» приложение передает координаты пациента (lat, lon): из "
                     "геолокации телефона или из адреса, который пациент ввел вручную. Тогда в ответе "
                     "заполняется distanceKm, а врачи сортируются по расстоянию до клиники. Без координат "
                     "сортировка по ближайшему свободному слоту. Доступно без авторизации.",
         responses=err(400, "Для поиска рядом нужны обе координаты: lat и lon", "COORDINATES_REQUIRED"))
async def list_doctors(
    specialty: Optional[str] = Query(None, description="Специальность", examples=["терапевт"]),
    clinicId: Optional[int] = Query(None, description="Клиника", examples=[1]),
    date_: Optional[date] = Query(None, alias="date", description="Показать ближайший слот на эту дату"),
    lat: Optional[float] = Query(None, ge=-90, le=90, description="Широта пациента", examples=[55.7558]),
    lon: Optional[float] = Query(None, ge=-180, le=180, description="Долгота пациента", examples=[37.6173]),
    radiusKm: Optional[float] = Query(None, gt=0, description="Показать клиники не дальше N км (нужны lat и lon)",
                                      examples=[5]),
):
    if (lat is None) != (lon is None) or (radiusKm is not None and lat is None):
        raise ApiError(400, "COORDINATES_REQUIRED", "Для поиска рядом нужны обе координаты: lat и lon")
    store.expire_holds()
    result = []
    for d in store.doctors.values():
        if specialty and d["specialty"].lower() != specialty.lower():
            continue
        if clinicId and d["clinicId"] != clinicId:
            continue
        free = sorted(s["startAt"] for s in store.slots.values()
                      if s["doctorId"] == d["id"] and s["isAvailable"] and s["startAt"] > now()
                      and (date_ is None or s["startAt"].date() == date_))
        clinic = store.clinics[d["clinicId"]]
        dist = distance_km(lat, lon, clinic.latitude, clinic.longitude) if lat is not None else None
        if radiusKm is not None and dist > radiusKm:
            continue
        result.append(Doctor(id=d["id"], fullName=d["fullName"], specialty=d["specialty"],
                             clinic=clinic, price=d["price"], rating=d["rating"],
                             nearestSlotAt=free[0] if free else None, distanceKm=dist))
    by_slot = lambda x: (x.nearestSlotAt is None, x.nearestSlotAt or now())
    if lat is not None:
        return sorted(result, key=lambda x: (x.distanceKm, *by_slot(x)))
    return sorted(result, key=by_slot)


# 2. Слоты врача
@app.get("/api/v1/doctors/{doctorId}/slots", tags=["Каталог"], response_model=list[Slot],
         summary="Получить расписание врача на дату",
         description="Возвращает все будущие слоты врача на выбранную дату, свободные и занятые. "
                     "Доступно без авторизации.",
         responses=err(404, "Врач не найден", "DOCTOR_NOT_FOUND"))
async def list_slots(
    doctorId: int = Path(description="Идентификатор врача", examples=[1]),
    date_: date = Query(alias="date", description="Дата (YYYY-MM-DD)"),
):
    if doctorId not in store.doctors:
        raise ApiError(404, "DOCTOR_NOT_FOUND", "Врач не найден")
    store.expire_holds()
    slots = [Slot(**s) for s in store.slots.values()
             if s["doctorId"] == doctorId and s["startAt"].date() == date_ and s["startAt"] > now()]
    return sorted(slots, key=lambda s: s.startAt)


# 3. Создать запись
@app.post("/api/v1/appointments", tags=["Записи"], response_model=Appointment, status_code=201,
          summary="Записаться на прием",
          description=f"Создает запись в статусе RESERVED и удерживает слот на {HOLD_MINUTES} минут. "
                      "Если за это время запись не оплачена, она переходит в EXPIRED, а слот освобождается.",
          responses={**R401, **R403, **err(400, "Слот уже прошел"), **err(404, "Слот не найден", "SLOT_NOT_FOUND"),
                     **err(409, "Слот уже занят", "SLOT_UNAVAILABLE")})
async def create_appointment(body: AppointmentCreate, user: dict = Depends(patient_user)):
    store.expire_holds()
    slot = take_slot(body.slotId)
    doc = store.doctors[slot["doctorId"]]
    aid = next(store._appt_ids)
    appt = dict(id=aid, patientId=user["patientId"], doctorId=doc["id"], clinicId=doc["clinicId"],
                slotId=slot["id"], startAt=slot["startAt"], cabinet=slot["cabinet"], reason=body.reason,
                price=doc["price"], status="RESERVED", bookingRevision=1,
                holdExpiresAt=now() + timedelta(minutes=HOLD_MINUTES),
                payment=None, cancelledBy=None, cancelReason=None, record=None,
                createdAt=now(), updatedAt=now())
    store.appointments[aid] = appt
    return store.appointment_out(appt)


# 4. Получить запись
@app.get("/api/v1/appointments/{appointmentId}", tags=["Записи"], response_model=Appointment,
         summary="Получить запись",
         description="Доступно пациенту-владельцу записи, врачу этого приема и администратору клиники. "
                     "Администратор получает запись без медицинских данных: reason и record равны null.",
         responses={**R401, **R403, **err(404, "Запись не найдена")})
async def get_appointment_endpoint(appointmentId: int = Path(examples=[1001]), user: dict = Depends(current_user)):
    a = get_appointment(appointmentId)
    allowed = ((user["role"] == "patient" and a["patientId"] == user["patientId"]) or
               (user["role"] == "doctor" and a["doctorId"] == user["doctorId"]) or
               (user["role"] == "admin" and a["clinicId"] == user["clinicId"]))
    if not allowed:
        raise ApiError(403, "FORBIDDEN", "Нет доступа к этой записи")
    return store.appointment_out(a, user)


# 5. Перенести запись
@app.patch("/api/v1/appointments/{appointmentId}", tags=["Записи"], response_model=Appointment,
           summary="Перенести запись на другой слот",
           description=f"Переносит запись на другой свободный слот того же врача. Возможно для записей "
                       f"RESERVED и CONFIRMED не позднее чем за {CHANGE_DEADLINE_HOURS} часа до приема. "
                       "Старый слот освобождается, оплата сохраняется: цена у того же врача не меняется, "
                       "поэтому перенос проходит без доплаты и без возврата. Подтвержденная запись после "
                       "переноса переходит в PAID: новое время снова должна подтвердить клиника (МИС). "
                       "Пока клиника не подтвердила запись (PAID), перенести ее нельзя.",
           responses={**R401, **R403, **err(400, "Слот другого врача или в прошлом"),
                      **err(404, "Запись или слот не найдены"),
                      **err(409, "Слот занят, неподходящий статус или слишком поздно")})
async def reschedule_appointment(body: AppointmentReschedule, appointmentId: int = Path(examples=[1001]),
                           user: dict = Depends(patient_user)):
    a = own_appointment(appointmentId, user)
    if a["status"] == "PAID":
        raise ApiError(409, "AWAITING_CLINIC_CONFIRMATION",
                       "Запись еще ждет подтверждения клиникой, перенос станет доступен после него")
    check_change_deadline(a)
    new_slot = take_slot(body.slotId, doctor_id=a["doctorId"])
    store.slots[a["slotId"]]["isAvailable"] = True
    a.update(slotId=new_slot["id"], startAt=new_slot["startAt"], cabinet=new_slot["cabinet"],
             bookingRevision=a["bookingRevision"] + 1, updatedAt=now())
    if a["status"] == "CONFIRMED":            # новое время передается в МИС и ждет подтверждения
        a.update(status="PAID", misAppointmentId=None)
    return store.appointment_out(a)


# 6. Отменить запись
@app.delete("/api/v1/appointments/{appointmentId}", tags=["Записи"], response_model=Appointment,
            summary="Отменить запись",
            description=(
                "Отменяет запись до начала приема и освобождает слот. Правила возврата:\n"
                f"* пациент отменяет за {CHANGE_DEADLINE_HOURS} часа и более - возврат 100%;\n"
                f"* пациент отменяет позже чем за {CHANGE_DEADLINE_HOURS} часа - отмена возможна, но деньги "
                "не возвращаются (refundAmount = 0), приложение заранее предупреждает об этом;\n"
                "* администратор отменяет прием по инициативе клиники (болезнь врача, изменение графика) - "
                "возврат 100% в любое время, причина обязательна.\n\n"
                "При возврате статус платежа становится REFUND_PENDING, после подтверждения шлюза - REFUNDED. "
                "Неоплаченная запись (RESERVED) отменяется без платежных операций."),
            responses={**R401, **R403, **err(404, "Запись не найдена"),
                       **err(409, "Прием уже начался или запись не активна"),
                       **err(422, "Администратор не указал причину")})
async def cancel_appointment(appointmentId: int = Path(examples=[1001]),
                       reason: Optional[str] = Query(None, max_length=300, examples=["Врач заболел"],
                                                     description="Причина отмены. Обязательна для администратора"),
                       user: dict = Depends(current_user)):
    a = get_appointment(appointmentId)
    if user["role"] == "patient":
        if a["patientId"] != user["patientId"]:
            raise ApiError(403, "FORBIDDEN", "Это запись другого пациента")
        by_clinic = False
    elif user["role"] == "admin" and user["clinicId"] == a["clinicId"]:
        if not reason:
            raise ApiError(422, "REASON_REQUIRED", "Укажите причину отмены для пациента")
        by_clinic = True
    else:
        raise ApiError(403, "FORBIDDEN", "Отменить запись может пациент или администратор ее клиники")
    check_active(a)
    store.slots[a["slotId"]]["isAvailable"] = True
    pay = a["payment"]
    if pay and pay["status"] == "SUCCEEDED":
        early = a["startAt"] - now() >= timedelta(hours=CHANGE_DEADLINE_HOURS)
        if by_clinic or early:
            pay.update(status="REFUND_PENDING", refundAmount=pay["amount"])
        else:
            pay["refundAmount"] = 0
    a.update(status="CANCELLED", holdExpiresAt=None, cancelledBy="CLINIC" if by_clinic else "PATIENT",
             cancelReason=reason, updatedAt=now())
    return store.appointment_out(a, user)


# 7. Создать платеж
@app.post("/api/v1/appointments/{appointmentId}/payment", tags=["Оплата"], response_model=PaymentInfo,
          status_code=201, summary="Создать платеж за прием",
          description="Создает платеж в платежном шлюзе и возвращает ссылку на платежную форму. "
                      "Возможно только для записи в статусе RESERVED, пока не истек резерв слота.",
          responses={**R401, **R403, **err(404, "Запись не найдена"),
                     **err(409, "Запись не ожидает оплаты или платеж уже создан", "NOT_AWAITING_PAYMENT")})
async def create_payment(appointmentId: int = Path(examples=[1001]), user: dict = Depends(patient_user)):
    a = own_appointment(appointmentId, user)
    if a["status"] != "RESERVED":
        raise ApiError(409, "NOT_AWAITING_PAYMENT", f"Запись в статусе {a['status']} не ожидает оплаты")
    if a["payment"] and a["payment"]["status"] == "PENDING":
        raise ApiError(409, "PAYMENT_ALREADY_CREATED", "Платеж уже создан, перейдите по ссылке на оплату")
    pid = f"pay_{next(store._pay_ids):06d}"
    a["payment"] = dict(paymentId=pid, status="PENDING", amount=a["price"], refundAmount=None,
                        paymentUrl=f"https://pay.example/checkout/{pid}")
    a["updatedAt"] = now()
    store.payments[pid] = (a["id"], a["payment"])
    return PaymentInfo(**a["payment"])


# 8. Вебхук платежного шлюза
@app.post("/api/v1/payments/webhook", tags=["Оплата"], response_model=WebhookAck,
          summary="Принять результат оплаты от платежного шлюза",
          description="Вызывается платежным шлюзом, а не приложением. Подлинность проверяется по заголовку "
                      f"X-Webhook-Signature (в прототипе - `{WEBHOOK_SECRET}`). При успешной оплате запись "
                      "переходит в PAID («Оплачено, ожидает подтверждения клиникой») и передается в МИС. Если резерв к этому моменту истек или запись отменена, деньги "
                      "автоматически возвращаются. Событие refunded завершает возврат: REFUND_PENDING -> "
                      "REFUNDED. Повторная доставка того же события ничего не меняет.",
          responses={**err(401, "Неверная подпись вебхука", "INVALID_SIGNATURE"),
                     **err(404, "Платеж не найден", "PAYMENT_NOT_FOUND")})
async def payment_webhook(body: PaymentWebhook,
                    x_webhook_signature: str = Header(description="Подпись шлюза", examples=[WEBHOOK_SECRET])):
    if x_webhook_signature != WEBHOOK_SECRET:
        raise ApiError(401, "INVALID_SIGNATURE", "Неверная подпись вебхука")
    attempt = store.payments.get(body.paymentId)
    if attempt is None:
        raise ApiError(404, "PAYMENT_NOT_FOUND", "Платеж не найден")
    aid, pay = attempt
    a = get_appointment(aid)
    if body.status == "refunded":
        if pay["status"] == "REFUND_PENDING":
            pay["status"] = "REFUNDED"
            a["updatedAt"] = now()
        return WebhookAck()                   # повтор или возврат не запускали - ничего не меняем
    if pay["status"] != "PENDING":            # идемпотентность: событие уже обработано
        return WebhookAck()
    if body.status == "failed":
        pay["status"] = "FAILED"
    elif a["status"] == "RESERVED":
        pay["status"] = "SUCCEEDED"
        a.update(status="PAID", holdExpiresAt=None)
    else:                                       # оплата пришла после истечения резерва или отмены
        pay.update(status="REFUND_PENDING", refundAmount=pay["amount"])
    a["updatedAt"] = now()
    return WebhookAck()


# 9. История пациента
@app.get("/api/v1/patients/me/appointments", tags=["Пациент"], response_model=list[Appointment],
         summary="Мои записи и заключения",
         description="Возвращает записи текущего пациента, сначала новые. У состоявшихся приемов "
                     "заполнено поле record с заключением врача из МИС.",
         responses={**R401, **R403})
async def my_appointments(status: Optional[AppointmentStatus] = Query(None, description="Фильтр по статусу"),
                    user: dict = Depends(patient_user)):
    store.expire_holds()
    items = [a for a in store.appointments.values()
             if a["patientId"] == user["patientId"] and (status is None or a["status"] == status)]
    return [store.appointment_out(a) for a in sorted(items, key=lambda a: a["startAt"], reverse=True)]


# 10. Записи клиники на день
@app.get("/api/v1/clinics/{clinicId}/appointments", tags=["Клиника"], response_model=list[ClinicAppointment],
         summary="Записи клиники на день",
         description="Для врача - его приемы на дату с причиной обращения. Для администратора - все "
                     "приемы клиники со статусами оплаты, но без причины обращения: это медицинские данные, "
                     "администратору они не нужны. Отмененные и просроченные записи тоже возвращаются, чтобы "
                     "было видно освободившиеся слоты.",
         responses={**R401, **R403, **err(404, "Клиника не найдена", "CLINIC_NOT_FOUND")})
async def clinic_appointments(clinicId: int = Path(examples=[1]),
                        date_: date = Query(alias="date", description="Дата (YYYY-MM-DD)"),
                        user: dict = Depends(current_user)):
    if clinicId not in store.clinics:
        raise ApiError(404, "CLINIC_NOT_FOUND", "Клиника не найдена")
    if user["role"] == "admin":
        if user["clinicId"] != clinicId:
            raise ApiError(403, "FORBIDDEN", "Администратор видит только свою клинику")
        doctor_filter = None
    elif user["role"] == "doctor":
        if store.doctors[user["doctorId"]]["clinicId"] != clinicId:
            raise ApiError(403, "FORBIDDEN", "Врач не работает в этой клинике")
        doctor_filter = user["doctorId"]
    else:
        raise ApiError(403, "FORBIDDEN", "Доступно врачу и администратору")
    store.expire_holds()
    items = [a for a in store.appointments.values()
             if a["clinicId"] == clinicId and a["startAt"].date() == date_
             and (doctor_filter is None or a["doctorId"] == doctor_filter)]
    is_doctor = user["role"] == "doctor"
    return [ClinicAppointment(id=a["id"], startAt=a["startAt"], cabinet=a["cabinet"],
                              doctor=store.doctor_ref(a["doctorId"]), patientId=a["patientId"],
                              patientName=store.patients[a["patientId"]],
                              reason=a["reason"] if is_doctor else None, status=a["status"],
                              paymentStatus=a["payment"]["status"] if a["payment"] else None)
            for a in sorted(items, key=lambda a: a["startAt"])]


# 11. Отметка явки
@app.patch("/api/v1/appointments/{appointmentId}/attendance", tags=["Клиника"], response_model=Appointment,
           summary="Отметить явку или неявку пациента",
           description="Администратор клиники отмечает, пришел ли пациент. Доступно для оплаченных записей "
                       "(CONFIRMED) после начала приема. ATTENDED переводит запись в COMPLETED, NO_SHOW - в NO_SHOW.",
           responses={**R401, **R403, **err(404, "Запись не найдена"),
                      **err(409, "Прием еще не начался или запись не подтверждена")})
async def mark_attendance(body: AttendanceUpdate, appointmentId: int = Path(examples=[1002]),
                    user: dict = Depends(current_user)):
    a = get_appointment(appointmentId)
    if user["role"] != "admin" or user["clinicId"] != a["clinicId"]:
        raise ApiError(403, "FORBIDDEN", "Отметка явки доступна администратору клиники")
    if a["status"] != "CONFIRMED":
        raise ApiError(409, "INVALID_STATUS", f"Запись в статусе {a['status']} нельзя отметить")
    if a["startAt"] > now():
        raise ApiError(409, "NOT_STARTED", "Прием еще не начался")
    a.update(status="COMPLETED" if body.attendance == "ATTENDED" else "NO_SHOW", updatedAt=now())
    return store.appointment_out(a, user)


# 12. Заключения пациента для врача
@app.get("/api/v1/patients/{patientId}/records", tags=["Клиника"], response_model=list[PatientRecord],
         summary="Заключения прошлых визитов пациента (для врача)",
         description="Врач видит заключения пациента по всем клиникам сети, но только если у этого пациента "
                     "есть подтвержденная запись к нему на сегодня или на будущие даты. Администратор "
                     "заключения не видит. Пациент смотрит свои заключения через /patients/me/appointments.",
         responses={**R401, **R403, **err(404, "Пациент не найден", "PATIENT_NOT_FOUND")})
async def patient_records(patientId: int = Path(examples=[1]), user: dict = Depends(current_user)):
    if user["role"] != "doctor":
        raise ApiError(403, "FORBIDDEN", "Заключения пациентов доступны только врачу")
    if patientId not in store.patients:
        raise ApiError(404, "PATIENT_NOT_FOUND", "Пациент не найден")
    store.expire_holds()
    has_visit = any(a["patientId"] == patientId and a["doctorId"] == user["doctorId"]
                    and a["status"] == "CONFIRMED" and a["startAt"].date() >= now().date()
                    for a in store.appointments.values())
    if not has_visit:
        raise ApiError(403, "NO_ACTIVE_APPOINTMENT", "У пациента нет подтвержденной записи к вам")
    items = [a for a in store.appointments.values() if a["patientId"] == patientId and a["record"]]
    return [PatientRecord(id=a["record"]["id"], appointmentId=a["id"], visitAt=a["startAt"],
                          doctor=store.doctor_ref(a["doctorId"]), conclusion=a["record"]["conclusion"],
                          fileUrl=a["record"]["fileUrl"])
            for a in sorted(items, key=lambda a: a["startAt"], reverse=True)]


# 13. Ответ МИС (заглушка integration-service)
@app.post("/api/v1/integrations/mis/booking-result", tags=["Интеграция"], response_model=WebhookAck,
          summary="Принять ответ МИС на передачу записи",
          description="Служебный метод: в целевой архитектуре integration-service публикует события mis.confirmed "
                      "и mis.rejected, в прототипе их заменяет этот вызов. Заголовок X-Internal-Token "
                      f"(в прототипе - `{MIS_TOKEN}`).\n"
                      "* confirmed: запись PAID переходит в CONFIRMED, сохраняется номер записи в МИС;\n"
                      "* rejected (UC 4): время уже занято в клинике - запись отменяется с отметкой SYSTEM, "
                      "запускается полный возврат.\n\n"
                      "bookingRevision должна совпадать с версией переданной записи. Ответ для другой версии "
                      "или записи не в статусе PAID (повтор, запись уже отменена) ничего не меняет.",
          responses={**err(401, "Неверный служебный токен", "INVALID_INTERNAL_TOKEN"),
                     **err(404, "Запись не найдена")})
async def mis_booking_result(body: MisBookingResult,
                       x_internal_token: str = Header(description="Служебный токен", examples=[MIS_TOKEN])):
    if x_internal_token != MIS_TOKEN:
        raise ApiError(401, "INVALID_INTERNAL_TOKEN", "Неверный служебный токен")
    a = get_appointment(body.appointmentId)
    if a["status"] != "PAID" or body.bookingRevision != a["bookingRevision"]:
        return WebhookAck()
    if body.result == "confirmed":
        a.update(status="CONFIRMED", misAppointmentId=body.misAppointmentId, updatedAt=now())
    else:
        pay = a["payment"]
        if pay and pay["status"] == "SUCCEEDED":
            pay.update(status="REFUND_PENDING", refundAmount=pay["amount"])
        a.update(status="CANCELLED", cancelledBy="SYSTEM",
                 cancelReason="Время уже занято в клинике, деньги возвращаются полностью", updatedAt=now())
    return WebhookAck()
