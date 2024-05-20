from typing import Dict, List, Optional, Tuple, Union


class PatientMetadata:
    def __init__(self, patient_metadata: Dict[str, Union[str, None]]):
        '''
        should have the following keys:
        given_name, family_name,
        administrative_gender_code, birth_time,
        phone_number, street_address_line, city, state, postal_code, country
        '''
        self.__dict__.update(patient_metadata)

    def get_dict(self) -> Dict[str, Union[str, None]]:
        return self.__dict__

    def get_dict_for_iti55(self) -> Dict[str, Union[str, None]]:
        return {
            'patient_given_name': self.given_name,
            'patient_family_name': self.family_name,
            'date_of_birth': self.birth_time,
            'gender': self.administrative_gender_code,
            'patient_address_street': self.street_address_line,
            'patient_address_city': self.city,
            'patient_address_state': self.state,
            'patient_address_postal_code': self.postal_code,
            'patient_address_country': self.country,
            'patient_phone': self.phone_number,
            'patient_email': self.email
        }
