"""Native dialogs used by the clan lounge."""
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256

import streamlit as st

from roly.ui import perform

KST = timezone(timedelta(hours=9))
WEEKDAYS = ('월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일')


def local_time(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(KST)
    except (TypeError, ValueError):
        return None


def time_label(value):
    result = local_time(value)
    return result.strftime('%Y.%m.%d %H:%M') if result else '시각 미등록'


def close_lounge_dialog():
    st.session_state.pop('home_dialog', None)
    st.session_state.pop('_clan_profile_editor', None)


def perform_in_dialog(action, message):
    def save():
        action()
        close_lounge_dialog()
    perform(save, message)


@st.dialog('클랜 소개 수정', width='medium', on_dismiss=close_lounge_dialog)
def profile_dialog(service, token):
    current = service.profile()
    actor = service.core.session(token)
    scope = sha256(f"{service.core.db_path}:{actor['id'] if actor else 'guest'}".encode()).hexdigest()[:16]
    editor = st.session_state.get('_clan_profile_editor')
    if not editor or editor['scope'] != scope:
        editor = {'scope': scope, 'profile': dict(current)}
        st.session_state['_clan_profile_editor'] = editor
    profile = editor['profile']
    version = sha256(profile['updated_at'].encode()).hexdigest()[:16]
    key = f'clan_profile_{scope}_{version}'
    if profile['updated_at'] != current['updated_at']:
        st.warning('클랜 소개가 변경됐거나 이전 저장이 완료됐습니다. 입력은 유지했습니다. 다시 저장하면 이미 반영된 내용인지 확인하고, 충돌한 변경은 저장하지 않습니다.')
        if st.button('최신 소개 불러오기', key=f'{key}_reload'):
            st.session_state['_clan_profile_editor'] = {'scope': scope, 'profile': dict(current)}
            st.rerun()
        st.caption('최신 소개를 불러오면 작성 중인 입력이 최신 저장값으로 바뀝니다.')
    with st.form('clan_profile_form'):
        name = st.text_input('클랜명', value=profile['name'], max_chars=60, key=f'{key}_name')
        description = st.text_area('소개', value=profile['description'], max_chars=2000, key=f'{key}_description')
        founded = st.date_input('개설일', value=date.fromisoformat(profile['founded_on']) if profile['founded_on'] else None,
                                min_value=date(2009, 1, 1), max_value=date.today(), format='YYYY.MM.DD', key=f'{key}_founded')
        capacity = st.number_input('회원 정원 (0 = 미설정)', min_value=0, max_value=10000, value=profile['capacity'] or 0, key=f'{key}_capacity')
        contact = st.text_input('연락 링크', value=profile['contact_url'], placeholder='https://', help='사이드바에 표시됩니다.', key=f'{key}_contact')
        if st.form_submit_button('소개 저장', type='primary'):
            perform_in_dialog(lambda: service.update_profile(token, name=name, description=description,
                    founded_on=founded.isoformat() if founded else '', capacity=capacity or None, contact_url=contact,
                    expected_updated_at=profile['updated_at']), '클랜 소개를 저장했습니다.')
