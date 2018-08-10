#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
destination.factory
'''

from app import app

from config import CFG
from utils.fmt import fmt
from utils.exceptions import AutocertError
from destination.aws import AwsDestination
from destination.zeus import ZeusDestination

class DestinationFactoryError(AutocertError):
    def __init__(self, destination):
        msg = fmt('destination factory error with {destination}')
        super(DestinationFactoryError, self).__init__(msg)

def create_destination(destination, ar, cfg, timeout, verbosity):
    d = None
    if destination == 'aws':
        d = AwsDestination(ar, cfg, verbosity)
    elif destination == 'zeus':
        d = ZeusDestination(ar, cfg, verbosity)
    else:
        raise DestinationFactoryError(destination)
    dests = list(CFG.destinations.zeus.keys())
    if d.has_connectivity(timeout, *dests):
        return d

