"""The AWS-only settings: the database URL built from parts, and sending through SES."""

import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import ANY, Stubber
from pydantic import ValidationError

from briefing_engine.config import Settings
from briefing_engine.delivery.email import (
    SendError,
    SesMailer,
    SmtpMailer,
    classify_ses_error,
    make_mailer,
)
from briefing_engine.delivery.render import RenderedEmail

EMAIL = RenderedEmail(subject="Your briefing", text="plain", html="<p>html</p>")


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_without_db_host_the_database_url_is_used_as_given() -> None:
    s = settings(database_url="postgresql+psycopg://a:b@localhost/x")
    assert s.database_url == "postgresql+psycopg://a:b@localhost/x"


def test_db_parts_build_an_escaped_tls_url() -> None:
    s = settings(db_host="db.example", db_user="briefing", db_password="p@ss/w:rd%")
    assert s.database_url == (
        "postgresql+psycopg://briefing:p%40ss%2Fw%3Ard%25@db.example:5432/briefing?sslmode=require"
    )


def test_db_host_without_a_password_is_refused() -> None:
    with pytest.raises(ValidationError, match="DB_PASSWORD"):
        settings(db_host="db.example")


def test_the_transport_picks_the_mailer() -> None:
    assert isinstance(make_mailer(settings()), SmtpMailer)
    s = settings(mail_transport="ses", aws_region="eu-west-1")
    assert isinstance(make_mailer(s), SesMailer)


def ses_mailer() -> tuple[SesMailer, Stubber]:
    import boto3

    client = boto3.client(
        "sesv2",
        region_name="eu-west-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    s = settings(mail_transport="ses", mail_from="Briefing <briefing@example.test>")
    return SesMailer(s, client=client), Stubber(client)


def test_ses_send_passes_the_whole_message_raw() -> None:
    mailer, stub = ses_mailer()
    stub.add_response(
        "send_email",
        {"MessageId": "ses-1"},
        {
            "Destination": {"ToAddresses": ["reader@example.test"]},
            "Content": {"Raw": {"Data": ANY}},
        },
    )
    with stub:
        mailer.send("reader@example.test", EMAIL, "<d1@briefing-engine.local>")
    stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    ("code", "permanent"),
    [
        ("MessageRejected", True),
        ("MailFromDomainNotVerifiedException", True),
        ("TooManyRequestsException", False),
        ("LimitExceededException", False),
        ("SendingPausedException", False),
    ],
)
def test_ses_errors_are_classified(code: str, permanent: bool) -> None:
    mailer, stub = ses_mailer()
    stub.add_client_error("send_email", service_error_code=code, service_message="nope")
    with stub, pytest.raises(SendError) as caught:
        mailer.send("reader@example.test", EMAIL, "<d1@briefing-engine.local>")
    assert caught.value.permanent is permanent
    assert code in str(caught.value)


def test_network_trouble_is_temporary() -> None:
    class Down:
        def send_email(self, **_: object) -> None:
            raise EndpointConnectionError(endpoint_url="https://email.eu-west-1.amazonaws.com")

    mailer = SesMailer(settings(mail_transport="ses"), client=Down())
    with pytest.raises(SendError) as caught:
        mailer.send("reader@example.test", EMAIL, "<d1@briefing-engine.local>")
    assert caught.value.permanent is False


def test_classify_keeps_messages_short() -> None:
    error = classify_ses_error("MessageRejected", "x" * 1000)
    assert len(str(error)) == 500
    assert error.permanent
