import json
import logging
import requests
from operator import itemgetter
from typing import List, Dict


from zabbix_cachet.excepltions import CachetApiException


def client_http_error(url, code, message):
    logging.error('ClientHttpError[%s, %s: %s]' % (url, code, message))


class Cachet:
    def __init__(self, server: str, token: str, verify=True):
        """
        Init Cachet class for further needs
        """
        self.server = server + '/api/'
        self.token = token
        self.headers = {'X-Cachet-Token': self.token, 'Authorization': f'Bearer {self.token}', 'Accept': 'application/json; indent=4'}
        self.verify = verify
        self.version = self.get_version()

    def _http_post(self, url, params):
        """
        Make POST and return json response
        :param url: str
        :param params: dict
        :return: json
        """
        url = self.server + url
        logging.debug("Sending to {url}: {param}".format(url=url,
                                                         param=json.dumps(params,
                                                                          indent=4,
                                                                          separators=(',', ': '))))
        try:
            r = requests.post(url=url, data=params, headers=self.headers, verify=self.verify)
        except requests.exceptions.RequestException as e:
            raise client_http_error(url, None, e)
        # r.raise_for_status()
        if not (200 <= r.status_code < 300):
            return client_http_error(url, r.status_code, r.text)
        try:
            r_json = json.loads(r.text)
        except ValueError:
            raise CachetApiException(f"Unable to parse json: {r.text}")
        logging.debug("Response Body: %s", json.dumps(r_json,
                                                      indent=4,
                                                      separators=(',', ': ')))
        return r_json

    def _http_get(self, url, params=None):
        """
        Helper for HTTP GET request
        :param: url: str
        :param: params:
        :return: json data
        """
        if params is None:
            params = {}
        url = self.server + url
        logging.debug("Sending to {url}: {param}".format(url=url,
                                                         param=json.dumps(params,
                                                                          indent=4,
                                                                          separators=(',', ': '))))
        try:
            r = requests.get(url=url, headers=self.headers, params=params, verify=self.verify)
        except requests.exceptions.RequestException as e:
            raise client_http_error(url, None, e)
        # r.raise_for_status()
        if r.status_code == 502:
            client_http_error(url, 502, "Bad Gateway")
            raise CachetApiException(f"Failed to get Cachet version. Probably it is not available")
        elif not (200 <= r.status_code < 300):
#            return client_http_error(url, r.status_code, json.loads(r.text)['errors'])
             body = json.loads(r.text or '{}')
             return client_http_error(
                 url, r.status_code,
                 body.get('errors') or body.get('message') or r.text)
        try:
            r_json = json.loads(r.text)
        except ValueError:
            raise CachetApiException(
                "Unable to parse json: %s" % r.text
            )
        logging.debug("Response Body: %s", json.dumps(r_json,
                                                      indent=4,
                                                      separators=(',', ': ')))
        return r_json

    def _http_put(self, url, params):
        """
        Make PUT and return json response
        :param url: str
        :param params: dict
        :return: json
        """
        url = self.server + url
        logging.debug("Sending to {url}: {param}".format(url=url,
                                                         param=json.dumps(params,
                                                                          indent=4,
                                                                          separators=(',', ': '))))
        try:
            r = requests.put(url=url, json=params, headers=self.headers, verify=self.verify)
        except requests.exceptions.RequestException as e:
            raise client_http_error(url, None, e)
        # r.raise_for_status()
        if not (200 <= r.status_code < 300):
            return client_http_error(url, r.status_code, r.text)
        try:
            r_json = json.loads(r.text)
        except ValueError:
            raise CachetApiException(
                "Unable to parse json: %s" % r.text
            )
        logging.debug("Response Body: %s", json.dumps(r_json,
                                                      indent=4,
                                                      separators=(',', ': ')))
        return r_json

    def get_version(self):
        """
        Get Cachet version for logging
        :return: str
        """
        url = 'version'
        data = self._http_get(url)
        return data['data']

    def get_component(self, id):
        """
        Get component params based its id
        @param id: string
        @return: dict
        """
        url = 'components/' + str(id)
        data = self._http_get(url)
        return data

    def get_components(self, name=None):
        """
        Get all registered components or return a component details if name specified
        Please note, it name was not defined method returns only last page of data
        :param name: Name of component to search
        :type name: str
        :return: Data =)
        :rtype: dict or list
        """
        url = 'components'
        data = self._http_get(url, {'include': 'group'})
        meta = data.get('meta', {})
        pag  = meta.get('pagination', {}) if isinstance(meta.get('pagination', {}), dict) else meta
        total_pages = int(pag.get('total_pages', pag.get('last_page', 1)))
        if name:
            components = []
            for page in range(total_pages, 0, -1):
                if page == 1:
                    data_page = data
                else:
                    data_page = self._http_get(url, params={'page': page, 'include': 'group'})
                for component in data_page['data']:
                    #if component['name'] == name:     # cachet v2
                    #    components.append(component)
                    comp_name = component.get('name') or component.get('attributes', {}).get('name')
                    if comp_name == name:
                        components.append(component)

            if len(components) < 1:
                return {'id': 0, 'name': 'Does not exists'}
            else:
                return components
        return data

    def _get_component_groupid(self, component_dict):
        """Extract group id from a component dict (v2 & v3)."""
        return (
            component_dict.get('group_id') or                       # Cachet 2.x
            component_dict.get('attributes', {}).get('group_id') or # rare in v3
            (component_dict.get('relationships', {})
             .get('group', {})
             .get('data', {}) or {})
             .get('id', 0)                            # v3 JSON:API
        )

    def new_components(self, name, **kwargs):
        """
        Create new components
        @param name: string
        @param kwargs: various additional values =)
        @return: dict of data
        """
        # Get values for new component
        params = {'name': name, 'link': '', 'description': '', 'status': '1', 'component_group_id': 0}
        params.update(kwargs)
        # Do not post empty params to Cachet
        for i in ('link', 'description'):
            # Strip params to avoid empty (' ') values #24
            if str(params[i]).strip() == '':
                params.pop(i)
        # Check if components with same name already exists in same group
        component = self.get_components(name)
        # There are more that one component with same name already
        if isinstance(component, list):
            for i in component:
                if self._get_component_groupid(i) == params['component_group_id']:
                    return i
        elif isinstance(component, dict):
            if component.get('id') and self._get_component_groupid(component) == params['component_group_id']:
                return component

        # component_group_id 0 (meaning no group) isn't allowed in cachet v3 anymore
        if params['component_group_id'] == 0:
            params.pop('component_group_id')

        # Create component if it does not exist or exist in other group
        url = 'components'
        # params = {'name': name, 'link': link, 'description': description, 'status': status}
        logging.debug('Creating Cachet component {name}...'.format(name=params['name']))
        data = self._http_post(url, params)

        logging.info('Component {name} created in group {gid}.'.format(name=params['name'], gid=self._get_component_groupid(data['data'])))

        return data['data']

    def upd_components(self, id, **kwargs):
        """
        Update component
        @param id: string
        @param kwargs: various additional values =)
        @return: boolean
        """
        url = 'components/' + str(id)
        params = self.get_component(id)['data']
        params.update(kwargs)
        data = self._http_put(url, params)
        if data:
            logging.info('Component {name} (id={id}) was updated. Status - {status}'.format(
                name=data['data']['attributes']['name'],
                id=id,
                status=data['data']['attributes']['status']['human']))
        return data

    def get_components_gr(self, name=None):
        """
        Get all registered components group or return a component group details if name specified
        Please note, this name was not defined method returns only last page of data
        @param name: string
        @return: dict of data
        """
        url = 'component-groups'
        data = self._http_get(url)
        meta = data.get('meta', {})
        pag  = meta.get('pagination', {}) if isinstance(meta.get('pagination', {}), dict) else meta
        total_pages = int(pag.get('total_pages', pag.get('last_page', 1)))
        if name:
            for page in range(total_pages, 0, -1):
                if page == 1:
                    data_page = data
                else:
                    data_page = self._http_get(url, params={'page': page})
                for group in data_page['data']:
                    grp_name = group.get('name') or group.get('attributes', {}).get('name')
                    if grp_name == name:
                        return group
            return {'id': 0, 'name': 'Does not exists'}
        return data

    def new_components_gr(self, name: str):
        """
        Create new components group
        @param name: string
        @return: dict of data
        """
        # Check if component's group already exists
        components_gr_id = self.get_components_gr(name)
        if components_gr_id['id'] == 0:
            url = 'component-groups'
            # TODO: Sadly the API doesn't allow a 'collapsed' value. Maybe check the docs in the future
            params = {'name': name, 'visible': 1}
            logging.debug('Creating Component Group {}...'.format(params['name']))
            data = self._http_post(url, params)
            if 'data' in data:
                logging.info('Component Group {} was created ({})'.format(params['name'], data['data']['id']))
            return data['data']
        else:
            return components_gr_id

    def get_incidents(self, component_id: int) -> List[Dict]:
        """
        Get incidents for component_id
        @param component_id: int
        @return: dict of data
        """
        # TODO: make search by name
        url = 'incidents'
        data = self._http_get(url)
        meta = data.get('meta', {})
        pag  = meta.get('pagination', {}) if isinstance(meta.get('pagination', {}), dict) else meta
        total_pages = int(pag.get('total_pages', pag.get('last_page', 1)))

        incidents = []
        for page in range(total_pages, 0, -1):
            data = self._http_get(url, params={'page': page})
            data_sorted = sorted(data['data'], key=itemgetter('id'), reverse=True)
            for incident in data_sorted:
                if str(incident['attributes']['component_id']) == str(component_id):
                    # Convert status to str
                    incident['attributes']['status']['value'] = str(incident['attributes']['status']['value'])
                    incidents.append(incident)
        return incidents

    def new_incidents(self, **kwargs):
        """
        Create a new incident.
        @param kwargs: various additional values =)
                        name, message, status,
                        component_id, component_status
        @return: dict of data
        """
        params = {'visible': 1, 'notify': 'true'}
        url = 'incidents'
        params.update(kwargs)
        data = self._http_post(url, params)
        logging.info('Incident {name} (id={incident_id}) was created for component id {component_id}.'.format(
            name=params['name'],
            incident_id=data['data']['id'],
            component_id=params['component_id']))
        return data['data']

    def upd_incident(self, id, **kwargs):
        """
        Update incident
        @param id: string
        @param kwargs: various additional values =)
                message, status,
                component_status
        @return: boolean
        """
        # TODO: component_status doesn't exist in the API. Check the docs in the future
        url = 'incidents/' + str(id)
        params = kwargs
        data = self._http_put(url, params)
        logging.info(f"Incident ID {id} was updated. Status - {data['data']['attributes']['status']['human']}")
        return data
