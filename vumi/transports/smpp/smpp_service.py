from functools import wraps

from twisted.internet.defer import inlineCallbacks, returnValue, succeed

from vumi.reconnecting_client import ReconnectingClientService
from vumi.transports.smpp.protocol import (
    EsmeProtocol, EsmeProtocolFactory, EsmeProtocolError)
from vumi.transports.smpp.sequence import RedisSequence


GSM_MAX_SMS_BYTES = 140
GSM_MAX_SMS_7BIT_CHARS = 160


def proxy_protocol(func):
    @wraps(func)
    def wrapper(self, *args, **kw):
        protocol = self.get_protocol()
        if protocol is None:
            raise EsmeProtocolError('%s called while not connected.' % (func,))
        return func(self, protocol, *args, **kw)
    return wrapper


class SmppService(ReconnectingClientService):

    throttle_statuses = ('ESME_RTHROTTLED', 'ESME_RMSGQFUL')

    def __init__(self, endpoint, bind_type, transport):
        self.transport = transport
        self.transport_name = transport.transport_name
        self.message_stash = self.transport.message_stash
        self.deliver_sm_processor = self.transport.deliver_sm_processor
        self.dr_processor = self.transport.dr_processor
        self.sequence_generator = RedisSequence(transport.redis)

        factory = EsmeProtocolFactory(self, bind_type)
        ReconnectingClientService.__init__(self, endpoint, factory)

    def get_protocol(self):
        return self._protocol

    def get_bind_state(self):
        if self._protocol is None:
            return EsmeProtocol.CLOSED_STATE
        return self._protocol.state

    def is_bound(self):
        if self._protocol is not None:
            return self._protocol.is_bound()
        return False

    def stopService(self):
        d = succeed(None)
        if self._protocol is not None:
            d.addCallback(lambda _: self._protocol.disconnect())
        d.addCallback(lambda _: ReconnectingClientService.stopService(self))
        return d

    def get_config(self):
        return self.transport.get_static_config()

    def on_smpp_bind(self):
        return self.transport.unpause_connectors()

    def on_connection_lost(self):
        return self.transport.pause_connectors()

    def handle_submit_sm_resp(self, message_id, smpp_message_id, pdu_status):
        if pdu_status in self.throttle_statuses:
            return self.handle_submit_sm_throttled(message_id)
        func = self.transport.handle_submit_sm_failure
        if pdu_status == 'ESME_ROK':
            func = self.transport.handle_submit_sm_success
        return func(message_id, smpp_message_id, pdu_status)

    def handle_submit_sm_throttled(self, message_id):
        return self.transport.handle_submit_sm_throttled(message_id)

    @proxy_protocol
    def submit_sm(self, protocol, *args, **kw):
        """
        See :meth:`EsmeProtocol.submit_sm`.
        """
        return protocol.submit_sm(*args, **kw)

    def submit_sm_long(self, vumi_message_id, destination_addr, long_message,
                       **pdu_params):
        """
        Send a `submit_sm` command with the message encoded in the
        ``message_payload`` optional parameter.

        Same parameters apply as for ``submit_sm`` with the exception
        that the ``short_message`` keyword argument is disallowed
        because it conflicts with the ``long_message`` field.

        :returns: list of 1 sequence number, int.
        :rtype: list

        """
        if 'short_message' in pdu_params:
            raise EsmeProtocolError(
                'short_message not allowed when sending a long message'
                'in the message_payload')

        optional_parameters = pdu_params.pop('optional_parameters', {}).copy()
        optional_parameters.update({
            'message_payload': (
                ''.join('%02x' % ord(c) for c in long_message))
        })
        return self.submit_sm(
            vumi_message_id, destination_addr, short_message='', sm_length=0,
            optional_parameters=optional_parameters, **pdu_params)

    def _fits_in_one_message(self, message):
        if len(message) <= GSM_MAX_SMS_BYTES:
            return True

        # NOTE: We already have byte strings here, so we assume that printable
        #       ASCII characters are all the same as single-width GSM 03.38
        #       characters.
        if len(message) <= GSM_MAX_SMS_7BIT_CHARS:
            # TODO: We need better character handling and counting stuff.
            return all(0x20 <= ord(ch) <= 0x7f for ch in message)

        return False

    def csm_split_message(self, message):
        """
        Chop the message into 130 byte chunks to leave 10 bytes for the
        user data header the SMSC is presumably going to add for us. This is
        a guess based mostly on optimism and the hope that we'll never have
        to deal with this stuff in production.

        NOTE: If we have utf-8 encoded data, we might break in the
              middle of a multibyte character. This should be ok since
              the message is only decoded after re-assembly of all
              individual segments.

        :param str message:
            The message to split
        :returns: list of strings
        :rtype: list

        """
        if self._fits_in_one_message(message):
            return [message]

        payload_length = GSM_MAX_SMS_BYTES - 10
        split_msg = []
        while message:
            split_msg.append(message[:payload_length])
            message = message[payload_length:]
        return split_msg

    @inlineCallbacks
    def submit_csm_sar(self, vumi_message_id, destination_addr, **pdu_params):
        """
        Submit a concatenated SMS to the SMSC using the optional
        SAR parameter names in the various PDUS.

        :returns: List of sequence numbers (int) for each of the segments.
        :rtype: list
        """

        split_msg = self.csm_split_message(pdu_params.pop('short_message'))

        if len(split_msg) == 1:
            # There is only one part, so send it without SAR stuff.
            sequence_numbers = yield self.submit_sm(
                vumi_message_id, destination_addr, short_message=split_msg[0],
                **pdu_params)
            returnValue(sequence_numbers)

        optional_parameters = pdu_params.pop('optional_parameters', {}).copy()
        ref_num = yield self.sequence_generator.next()
        sequence_numbers = []
        yield self.message_stash.init_multipart_info(
            vumi_message_id, len(split_msg))
        for i, msg in enumerate(split_msg):
            pdu_params = pdu_params.copy()
            optional_parameters.update({
                # Reference number must be between 00 & FFFF
                'sar_msg_ref_num': (ref_num % 0xFFFF),
                'sar_total_segments': len(split_msg),
                'sar_segment_seqnum': i + 1,
            })
            sequence_number = yield self.submit_sm(
                vumi_message_id, destination_addr, short_message=msg,
                optional_parameters=optional_parameters, **pdu_params)
            sequence_numbers.extend(sequence_number)
        returnValue(sequence_numbers)

    @inlineCallbacks
    def submit_csm_udh(self, vumi_message_id, destination_addr, **pdu_params):
        """
        Submit a concatenated SMS to the SMSC using user data headers (UDH)
        in the message content.

        Same parameters apply as for ``submit_sm`` with the exception
        that the ``esm_class`` keyword argument is disallowed
        because the SMPP spec mandates a value that is to be set for UDH.

        :returns: List of sequence numbers (int) for each of the segments.
        :rtype: list
        """

        if 'esm_class' in pdu_params:
            raise EsmeProtocolError(
                'Cannot specify esm_class, GSM spec sets this at 0x40 '
                'for concatenated messages using UDH.')

        pdu_params = pdu_params.copy()
        split_msg = self.csm_split_message(pdu_params.pop('short_message'))

        if len(split_msg) == 1:
            # There is only one part, so send it without UDH stuff.
            sequence_numbers = yield self.submit_sm(
                vumi_message_id, destination_addr, short_message=split_msg[0],
                **pdu_params)
            returnValue(sequence_numbers)

        ref_num = yield self.sequence_generator.next()
        sequence_numbers = []
        yield self.message_stash.init_multipart_info(
            vumi_message_id, len(split_msg))
        for i, msg in enumerate(split_msg):
            # 0x40 is the UDHI flag indicating that this payload contains a
            # user data header.

            # NOTE: Looking at the SMPP specs I can find no requirement
            #       for this anywhere.
            pdu_params['esm_class'] = 0x40

            # See http://en.wikipedia.org/wiki/User_Data_Header and
            # http://en.wikipedia.org/wiki/Concatenated_SMS for an
            # explanation of the magic numbers below. We should probably
            # abstract this out into a class that makes it less magic and
            # opaque.
            udh = ''.join([
                '\05',  # Full UDH header length
                '\00',  # Information Element Identifier for Concatenated SMS
                '\03',  # header length
                # Reference number must be between 00 & FF
                chr(ref_num % 0xFF),
                chr(len(split_msg)),
                chr(i + 1),
            ])
            short_message = udh + msg
            sequence_number = yield self.submit_sm(
                vumi_message_id, destination_addr, short_message=short_message,
                **pdu_params)
            sequence_numbers.extend(sequence_number)
        returnValue(sequence_numbers)