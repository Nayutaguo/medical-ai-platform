from decimal import Decimal

from medical_ai.data import DataQualityProfile, clean_sparcs_row, format_cleaned_row_for_csv


def test_clean_sparcs_row_normalizes_real_values() -> None:
    row = {
        "Hospital Service Area": "New York City",
        "Hospital County": "Bronx",
        "Operating Certificate Number": "7000006",
        "Permanent Facility Id": "001169",
        "Facility Name": "Montefiore Medical Center",
        "Age Group": "70 or Older",
        "Zip Code - 3 digits": "104",
        "Gender": "F",
        "Race": "White",
        "Ethnicity": "Not Span/Hispanic",
        "Length of Stay": "120 +",
        "Type of Admission": "Emergency",
        "Patient Disposition": "Home or Self Care",
        "Discharge Year": "2021",
        "CCSR Diagnosis Code": "CIR002",
        "CCSR Diagnosis Description": "Circulatory disease",
        "CCSR Procedure Code": "",
        "CCSR Procedure Description": "",
        "APR DRG Code": "194",
        "APR DRG Description": "Heart failure",
        "APR MDC Code": "5",
        "APR MDC Description": "Diseases of Circulatory System",
        "APR Severity of Illness Code": "3",
        "APR Severity of Illness Description": "Major",
        "APR Risk of Mortality": "Major",
        "APR Medical Surgical Description": "Medical",
        "Payment Typology 1": "Medicare",
        "Payment Typology 2": "",
        "Payment Typology 3": "",
        "Birth Weight": "",
        "Emergency Department Indicator": "Y",
        "Total Charges": "320,922.43",
        "Total Costs": "60,241.34",
    }

    cleaned = clean_sparcs_row(row)

    assert cleaned["AgeGroup"] == "70orOlder"
    assert cleaned["Gender"] == "Female"
    assert cleaned["LengthOfStay"] == 120
    assert cleaned["EmergencyDepartmentIndicator"] == "Yes"
    assert cleaned["TotalCharges"] == Decimal("320922.43")
    assert cleaned["RaceEthnicity"] == "White | Not Span/Hispanic"


def test_clean_sparcs_row_treats_unknown_integer_as_null() -> None:
    row = {
        "Length of Stay": "UNKN",
        "Discharge Year": "2021",
        "APR DRG Code": "UNKN",
        "APR MDC Code": "5",
        "APR Severity of Illness Code": "3",
        "Birth Weight": "",
    }

    cleaned = clean_sparcs_row(row)

    assert cleaned["LengthOfStay"] is None
    assert cleaned["APRDRGCode"] is None


def test_data_quality_profile_tracks_counts_ranges_and_lengths() -> None:
    cleaned = {
        "HospitalServiceArea": "New York City",
        "HospitalCounty": "Bronx",
        "OperatingCertificateNumber": "7000006",
        "PermanentFacilityId": "001169",
        "FacilityName": "Montefiore Medical Center",
        "AgeGroup": "70orOlder",
        "ZipCode3Digits": "104",
        "Gender": "Female",
        "Race": "White",
        "Ethnicity": "Not Span/Hispanic",
        "RaceEthnicity": "White | Not Span/Hispanic",
        "LengthOfStay": 120,
        "AdmissionType": "Emergency",
        "PatientDisposition": "Home or Self Care",
        "DischargeYear": 2021,
        "CCSRDiagnosisCode": "CIR002",
        "CCSRDiagnosisDescription": "Circulatory disease",
        "CCSRProcedureCode": None,
        "CCSRProcedureDescription": None,
        "APRDRGCode": 194,
        "APRDRGDescription": "Heart failure",
        "APRMDCCode": 5,
        "APRMDCDescription": "Diseases of Circulatory System",
        "APRSeverityOfIllnessCode": 3,
        "APRSeverityOfIllnessDescription": "Major",
        "APRRiskOfMortality": "Major",
        "APRMedicalSurgicalDescription": "Medical",
        "PaymentTypology1": "Medicare",
        "PaymentTypology2": None,
        "PaymentTypology3": None,
        "BirthWeight": None,
        "EmergencyDepartmentIndicator": "Yes",
        "TotalCharges": Decimal("320922.43"),
        "TotalCosts": Decimal("60241.34"),
    }

    profile = DataQualityProfile()
    profile.observe(cleaned)
    data = profile.to_dict()

    assert data["row_count"] == 1
    assert data["null_counts"]["CCSRProcedureCode"] == 1
    assert data["numeric_max"]["TotalCharges"] == "320922.43"
    assert data["top_values"]["AgeGroup"][0] == {"value": "70orOlder", "count": 1}
    assert format_cleaned_row_for_csv(cleaned)["CCSRProcedureCode"] == ""
