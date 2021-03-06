#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import io
import zipfile
from attrdict import AttrDict
from pprint import pprint, pformat
from fnmatch import fnmatch
from datetime import timedelta #FIXME: do we import this here?
from whois import whois
from tld import get_fld

from authority.base import AuthorityBase
from exceptions import AutocertError
from utils.dictionary import merge, body
from utils.newline import windows2unix
from app import app

def not_200(call):
    return call.recv.status != 200

def strip_wildcard(domain):
    return domain[2:] if domain.startswith('*.') else domain

class OrderCertificateError(AutocertError):
    def __init__(self, call):
        message = f'order certificate error call={call}'
        super(OrderCertificateError, self).__init__(message)

class RevokeCertificateError(AutocertError):
    def __init__(self, call):
        message = f'revoke certificate error call={call}'
        super(RevokeCertificateError, self).__init__(message)

class ApproveCertificateError(AutocertError):
    def __init__(self, call):
        message = f'approve certificate error call={call}'
        super(ApproveCertificateError, self).__init__(message)

class DownloadCertificateError(AutocertError):
    def __init__(self, call):
        message = f'download certificate error call={call}'
        super(DownloadCertificateError, self).__init__(message)

class OrganizationNameNotFoundError(AutocertError):
    def __init__(self, organization_name):
        message = f'organization name {organization_name} not found'
        super(OrganizationNameNotFoundError, self).__init__(message)

class NotValidatedDomainError(AutocertError):
    def __init__(self, denied_domains, active_domains):
        message = f'these denied_domains: {denied_domains} were not found in these active_domains: {active_domains}'
        super(NotValidatedDomainError, self).__init__(message)

class WhoisDoesntMatchError(AutocertError):
    def __init__(self, domains):
        domains = ', '.join(domains)
        message = f'list of domains with whois emails not matching hostmaster@mozilla.com: {domains}'
        super(WhoisDoesntMatchError, self).__init__(message)

class DigicertError(AutocertError):
    def __init__(self, call):
        message = 'digicert error without errors field'
        if 'errors' in call.recv.json:
            message = call.recv.json['errors'][0]['message']
        super(DigicertError, self).__init__(message)

def domain_to_check(domain):
    return domain if domain.startswith('*.') else get_fld('http://'+domain)

def expiryify(call):
    from utils.timestamp import string2datetime
    if call.recv.status != 200:
        raise DigicertError(call)
    try:
        valid_till = call.recv.json.certificate.valid_till
        if valid_till and valid_till != 'null':
            return string2datetime(valid_till)
    except AttributeError as ae:
        raise DigicertError(call)

def combine_sans(sans1, sans2):
    if sans1 is None:
        return list(sans2)
    elif sans2 is None:
        return list(sans1)
    return list(set(list(sans1) + list(sans2)))

class DigicertAuthority(AuthorityBase):
    def __init__(self, ar, cfg, verbosity):
        super(DigicertAuthority, self).__init__(ar, cfg, verbosity)

    def request(self, method, **kw):
        try:
            path = kw.pop('path')
            def next_offset(page):
                total = page.total
                limit = page.limit
                offset = page.offset + page.limit
                return offset if offset < page.total else None
            call = super(DigicertAuthority, self).request(method, path=path, **kw)
            offset = next_offset(call.recv.json.page)
            while call.recv.status in (200,) and offset:
                prev = call
                query_params = f'?offset={offset}'
                call = super(DigicertAuthority, self).request(method, path=path+query_params, **kw)
                offset = next_offset(call.recv.json.page)
                call.prev = prev
            return call
        except:
            import traceback
            traceback.print_exc()
            app.logger.debug(traceback.format_exc())

    def has_connectivity(self):
        call = self.get('user/me')
        if call.recv.status != 200:
            raise AuthorityConnectivityError(call)
        return True

    def display_certificates(self, bundles, repeat_delta=None):
        app.logger.info(f'display_certificates:\n{locals}')
        order_ids = [bundle.authority['digicert']['order_id'] for bundle in bundles]
        calls = self._get_certificate_order_detail(order_ids)
        certificate_ids = [call.recv.json.certificate.id for call in calls]
        crts = self._download_certificates(certificate_ids, repeat_delta=repeat_delta)
        expiries = [expiryify(call) for call in calls]
        csrs = [windows2unix(call.recv.json.certificate.csr) for call in calls]
        for expiry, csr, crt, bundle in zip(expiries, csrs, crts, bundles):
            matched = csr.strip() == bundle.csr.strip() and crt.strip() == bundle.crt.strip()
            bundle.authority['digicert']['matched'] = matched
        return bundles

    def create_certificate(self, organization_name, common_name, validity_years, csr, bug, sans=None, repeat_delta=None, whois_check=False):
        app.logger.info(f'create_certificate:\n{locals}')
        if not sans:
            sans = []
        organization_id, container_id = self._get_organization_container_ids(organization_name)
        path, json = self._prepare_path_json(
            organization_id,
            container_id,
            common_name,
            validity_years,
            csr,
            bug,
            sans=sans,
            whois_check=whois_check)
        crts, expiries, order_ids = self._create_certificates([path], [json], bug, repeat_delta)
        authority = dict(digicert=dict(order_id=order_ids[0]))
        return crts[0], expiries[0], authority

    def renew_certificates(self, bundles, organization_name, validity_years, bug, sans=None, repeat_delta=None, whois_check=False):
        app.logger.info(f'renew_certificates:\n{locals}')
        if not sans:
            sans = []
        organization_id, container_id = self._get_organization_container_ids(organization_name)
        paths, jsons = self._prepare_paths_jsons_for_renewals(
            bundles,
            organization_id,
            container_id,
            bug,
            validity_years,
            sans,
            whois_check)
        crts, expiries, order_ids = self._create_certificates(paths, jsons, bug, repeat_delta)
        authorities = [dict(digicert=dict(order_id=order_id)) for order_id in order_ids]
        return crts, expiries, authorities

    def revoke_certificates(self, bundles, bug):
        app.logger.info(f'revoke_certificates:\n{locals}')
        paths, jsons = self._prepare_paths_jsons_for_revocations(bundles, bug)
        self._revoke_certificates(paths, jsons, bug)
        return bundles

    def _get_organization_container_ids(self, organization_name):
        app.logger.debug(f'_get_organization_container_ids:\n{locals}')
        path = 'organization'
        call = self.get(path)
        if call.recv.status != 200:
            raise DigicertError(call)
        for organization in call.recv.json.organizations:
            if organization.name == organization_name:
                return organization.id, organization.container.id
        raise OrganizationNameNotFoundError(organization_name)

    def _get_domains(self, organization_id, container_id):
        app.logger.debug(f'_get_domains:\n{locals}')
        call = self.get(f'domain?container_id={container_id}')
        if call.recv.status != 200:
            raise DigicertError(call)
        return [domain for domain in call.recv.json.domains if domain.is_active and domain.organization.id == organization_id]

    def _validate_domains(self, organization_id, container_id, domains, whois_check=False):
        app.logger.debug(f'_validate_domains:\n{locals}')
        active_domains = self._get_domains(organization_id, container_id)
        active_domains = [ad.name for ad in active_domains]
        def _is_validated(domain):
            app.logger.debug(f'_is_validated:\n{locals}')
            fld = get_fld('http://'+domain)
            if domain in active_domains:
                return True
            elif fld in active_domains:
                return True
            return False
        def _whois_email(domain):
            app.logger.debug(f'_whois_email:\n{locals}')
            try:
                emails = whois(domain)['emails']
                app.logger.debug(f'emails={emails}')
                return 'hostmaster@mozilla.com' in emails
            except Exception as ex:
                app.logger.debug('WHOIS_ERROR')
                app.logger.debug(ex)
                return False
            return False
        not_whois_domains = []
        if whois_check:
            app.logger.info('the whois check was enabled with --whois-check flag for this run')
            not_whois_domains = [domain for domain in domains if not _whois_email(domain)]
        if not_whois_domains:
            raise WhoisDoesntMatchError(not_whois_domains)
        denied_domains = [domain for domain in domains if not _is_validated(domain)]
        if denied_domains:
            raise NotValidatedDomainError(denied_domains, active_domains)
        return True

    def _prepare_path_json(self, organization_id, container_id, common_name, validity_years, csr, bug, sans=None, whois_check=False, renewal_of_order_id=None):
        app.logger.debug(f'_prepare_path_json:\n{locals}')
        domains = list(set([common_name] + (sans if sans else [])))
        self._validate_domains(organization_id, container_id, domains, whois_check)
        path = 'order/certificate/ssl_plus'
        json = merge(self.cfg.template, dict(
            validity_years=validity_years,
            certificate=dict(
                common_name=common_name,
                csr=csr),
            organization=dict(
                id=organization_id),
            comments=bug))
        if common_name.startswith('*.'):
            path = 'order/certificate/ssl_wildcard'
        elif sans:
            path = 'order/certificate/ssl_multi_domain'
            json = merge(json, dict(
                certificate=dict(
                    dns_names=sans)))
        if renewal_of_order_id:
            json = merge(json, dict(
                renewal_of_order_id=renewal_of_order_id))
        return path, json

    def _prepare_paths_jsons_for_renewals(self, bundles, organization_id, container_id, bug, validity_years, sans_to_add, whois_check=False):
        app.logger.debug(f'_prepare_paths_jsons_for_renewals:\n{locals}')
        order_ids = [bundle.authority['digicert']['order_id'] for bundle in bundles]
        calls = self._get_certificate_order_detail(order_ids)
        paths = []
        jsons = []
        for bundle, call in zip(bundles, calls):
            bundle.sans=combine_sans(bundle.sans, sans_to_add)
            path, json = self._prepare_path_json(
                organization_id,
                container_id,
                bundle.common_name,
                validity_years,
                bundle.csr,
                bug,
                sans=bundle.sans,
                whois_check=whois_check,
                renewal_of_order_id=bundle.authority['digicert']['order_id'])
            paths += [path]
            jsons += [json]
        return paths, jsons

    def _prepare_paths_jsons_for_revocations(self, bundles, bug):
        app.logger.debug(f'_prepare_paths_jsons_for_revocations:\n{locals}')
        order_ids = [bundles.authority['digicert']['order_id'] for bundles in bundles]
        calls = self._get_certificate_order_detail(order_ids)
        certificate_ids = [call.recv.json.certificate.id for call in calls]
        paths = [f'certificate/{certificate_id}/revoke' for certificate_id in certificate_ids]
        jsons = [dict(comments=str(bug))]
        return paths, jsons

    def _create_certificates(self, paths, jsons, bug, repeat_delta):
        app.logger.debug(f'_create_certificates:\n{locals}')
        order_ids, request_ids = self._order_certificates(paths, jsons)
        self._update_requests_status(request_ids, 'approved', bug)
        calls = self._get_certificate_order_detail(order_ids)
        certificate_ids = [call.recv.json.certificate.id for call in calls]
        try:
            crts = self._download_certificates(certificate_ids, repeat_delta=repeat_delta)
            calls = self._get_certificate_order_detail(order_ids)
            expiries = [expiryify(call) for call in calls]
        except DownloadCertificateError as dce:
            app.logger.warning(str(dce))
            crts = []
            expiries = []
        return crts, expiries, order_ids

    def _revoke_certificates(self, paths, jsons, bug):
        app.logger.debug(f'_revoke_certificates:\n{locals}')
        calls = self.puts(paths=paths, jsons=jsons)
        for call in calls:
            if call.recv.status != 201:
                raise RevokeCertificateError(call)
        request_ids = [call.recv.json.id for call in calls]
        self._update_requests_status(request_ids, 'approved', bug)

    def _order_certificates(self, paths, jsons):
        app.logger.debug(f'_order_certificates:\n{locals}')
        calls = self.posts(paths=paths, jsons=jsons)
        for call in calls:
            if call.recv.status != 201:
                raise OrderCertificateError(call)
        return zip(*[(call.recv.json.id, call.recv.json.requests[0].id) for call in calls])

    def _update_requests_status(self, request_ids, status,bug):
        app.logger.debug(f'_update_requests_status:\n{locals}')
        paths = [f'request/{request_id}/status' for request_id in request_ids]
        jsons = [dict(status=status, processor_comment=bug)]
        app.logger.debug(f'calling digicert api with paths={paths} and jsons={jsons}')
        calls = self.puts(paths=paths, jsons=jsons)
        for call in calls:
            if call.recv.status != 204:
                if call.recv.json.errors[0].code != 'request_already_processed':
                    raise ApproveCertificateError(call)
        return True

    def _get_certificate_order_summary(self):
        app.logger.debug(f'_get_certificate_order_summary:\n{locals}')
        call = self.get(path='order/certificate')
        return call

    def _get_certificate_order_detail(self, order_ids):
        app.logger.debug(f'_get_certificate_order_detail:\n{locals}')
        paths = [f'order/certificate/{order_id}' for order_id in order_ids]
        calls = self.gets(paths=paths)
        return calls

    def _download_certificates(self, certificate_ids, format_type='pem_noroot', repeat_delta=None):
        app.logger.debug(f'_download_certificates:\n{locals}')
        if repeat_delta is not None and isinstance(repeat_delta, int):
            repeat_delta = timedelta(seconds=repeat_delta)
        paths = [f'certificate/{certificate_id}/download/format/{format_type}' for certificate_id in certificate_ids]
        calls = self.gets(paths=paths, repeat_delta=repeat_delta, repeat_if=not_200)
        texts = []
        for call in calls:
            if call.recv.status == 200:
                texts += [call.recv.text]
            else:
                raise DownloadCertificateError(call)
        return texts
