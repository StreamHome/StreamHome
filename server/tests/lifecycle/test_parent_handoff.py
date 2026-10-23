import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import select
import socket
import tempfile
import unittest
from unittest.mock import Mock,patch
import uuid

from lifecycle import state,restore_controller as restore
from lifecycle.control import parent_handoff as channel
from lifecycle.control.descriptor_transport import Binding,close_descriptors,parse_binding,receive_offer,send_offer
from lifecycle.control.rpc import ControlError


class ParentHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        (self.root/'server').mkdir()
        for name in ('start.sh','update.sh'):(self.root/name).write_text('#!/bin/sh\n')
        self.claim=state.claim_operation_with_credential(self.root,operation_type=state.OperationType.RESTORE,
                                                         owner_kind=state.OwnerKind.RESTORE_CONTROLLER)
        self.operation=self.claim.state.active_operation.operation_id
        restore.create_restore_journal(self.root,operation_id=self.operation,generation=1,
            candidate=restore.CandidateIdentity('backup_20260101_000000.db',128,'a'*64))
        self.binding=Binding(str(uuid.uuid4()),self.operation,1,'a'*32,str(uuid.uuid4()),'restore')
        self.pipes=[os.pipe() for _ in range(3)]
        self.addCleanup(lambda:close_descriptors(fd for pair in self.pipes for fd in pair))
        self.descriptors=self.pipes[0][0],self.pipes[1][1],self.pipes[2][0]
        self.starts=[]
        async def authorize(binding,peer):
            self.assertEqual(binding,self.binding);self.assertEqual(peer.uid,os.geteuid())
            state.validate_operation_with_credential(self.root,operation_id=binding.operation_id,
                generation=binding.generation,expected_phase=state.LifecyclePhase.RESERVED)
        async def start(binding):self.starts.append(binding)
        self.server=channel.Server(self.root/'parent.sock',authorize_offer=authorize,
                                   authorize_collect=authorize,start_operation=start)
        await self.server.start();self.addAsyncCleanup(self.server.close)

    def offer(self):
        with channel.connect(self.server.path,expected_uid=os.geteuid(),expected_pid=os.getpid()) as connection:
            channel.send_packet(connection,{'schema_version':1,'action':'offer','binding':self.binding.payload()})
            self.assertEqual(channel.receive_packet(connection),{'ready':True})
            send_offer(connection,self.binding,self.descriptors,expected_uid=os.geteuid(),expected_pid=os.getpid())
            self.assertEqual(channel.receive_packet(connection),{'custody':True,'request_id':self.binding.request_id})

    def collect(self):
        with channel.connect(self.server.path,expected_uid=os.geteuid(),expected_pid=os.getpid()) as connection:
            channel.send_packet(connection,{'schema_version':1,'action':'collect','operation_id':self.operation,
                'generation':1,'supervisor_invocation_id':'a'*32})
            binding=parse_binding(channel.receive_packet(connection)['binding'])
            self.assertEqual(binding,self.binding)
            channel.send_packet(connection,{'ready':True})
            return receive_offer(connection,binding,expected_uid=os.geteuid(),expected_pid=os.getpid())

    async def test_actual_custody_transfer_has_no_canonical_effect_and_is_one_use(self):
        before=state.inspect_state(self.root)
        await asyncio.to_thread(self.offer)
        self.assertEqual(state.inspect_state(self.root),before)
        self.assertEqual(self.starts,[self.binding])
        with self.assertRaises(ControlError):await asyncio.to_thread(self.offer)
        received=await asyncio.to_thread(self.collect)
        try:
            os.write(self.pipes[0][1],b'capability')
            self.assertEqual(os.read(received[0],64),b'capability')
            self.assertFalse(any(os.get_inheritable(fd) for fd in received))
        finally:close_descriptors(received)
        self.assertEqual(state.inspect_state(self.root),before)
        with self.assertRaises(ControlError):await asyncio.to_thread(self.collect)
        with self.assertRaises(ControlError):await asyncio.to_thread(self.offer)

    async def test_expiry_and_close_release_custody_without_takeover_or_start_retry(self):
        await asyncio.to_thread(self.offer)
        self.server.pending.expires_at=0;self.server._expire()
        with self.assertRaises(ControlError):await asyncio.to_thread(self.collect)
        self.assertEqual(state.inspect_state(self.root).generation,1)
        self.assertEqual(len(self.starts),1)

    async def test_unit_start_failure_closes_pending_channels(self):
        async def fail(binding):raise ControlError('control_controller_start_failed')
        self.server.start_operation=fail
        with self.assertRaises(ControlError):await asyncio.to_thread(self.offer)
        self.assertIsNone(self.server.pending)
        self.assertEqual(state.inspect_state(self.root).generation,1)

    async def test_canonical_generation_change_between_offer_and_collection_is_refused(self):
        await asyncio.to_thread(self.offer)
        state.transition_operation_with_credential(self.root,operation_id=self.operation,generation=1,
            expected_phase=state.LifecyclePhase.RESERVED,next_phase=state.LifecyclePhase.RECOVERY_REQUIRED)
        with self.assertRaises(ControlError):await asyncio.to_thread(self.collect)
        self.assertEqual(state.inspect_state(self.root).generation,1)

    async def _controller_gate_case(self,delivered):
        await asyncio.to_thread(self.offer)
        received=await asyncio.to_thread(self.collect)
        # Controller consumes the real capability/ACK/gate channels; the test
        # retains the ASGI-side gate writer until actual completion or EOF.
        os.write(self.pipes[0][1],(self.claim.owner_capability+'\n').encode())
        os.close(self.pipes[0][1]);self.pipes[0]=(self.pipes[0][0],-1)
        stop=Mock(side_effect=restore.RestoreRecoveryRequired('stop checkpoint reached'))
        def failed(paths,generation,error_code,web_port):
            restore._mark_recovery_required(paths,generation,error_code)
        def execute():
            cap,ack,gate=received
            capability=restore._read_pipe_secret(cap)
            return restore._run_controller(self.root,self.operation,1,capability,
                                           acknowledgement_fd=ack,gate_fd=gate)
        with patch.object(restore,'_stop_supported_runtime',stop),patch.object(restore,'_enter_fail_closed',side_effect=failed):
            task=asyncio.create_task(asyncio.to_thread(execute))
            self.assertTrue(await asyncio.to_thread(lambda:bool(select.select([self.pipes[1][0]],[],[],5)[0])))
            ack=json.loads(os.read(self.pipes[1][0],4096))
            self.assertEqual(ack,{'owned':True,'operation_id':self.operation,'generation':2})
            self.assertEqual(state.inspect_state(self.root).generation,2)
            stop.assert_not_called()
            if delivered:os.write(self.pipes[2][1],b'1')
            os.close(self.pipes[2][1]);self.pipes[2]=(self.pipes[2][0],-1)
            self.assertEqual(await asyncio.wait_for(task,5),1)
            if delivered:self.assertTrue(stop.called)
            else:stop.assert_not_called()
        self.assertEqual(state.inspect_state(self.root).active_operation.phase,state.LifecyclePhase.RECOVERY_REQUIRED)

    async def test_real_parent_transfer_ack_precedes_gate_and_only_delivered_response_allows_stop(self):
        await self._controller_gate_case(True)

    async def test_actual_response_gate_eof_never_allows_runtime_shutdown(self):
        await self._controller_gate_case(False)

    async def test_metadata_rejects_rights_without_leaking_kernel_duplicates(self):
        from array import array
        left,right=socket.socketpair(socket.AF_UNIX,socket.SOCK_SEQPACKET)
        with left,right:
            before=set(os.listdir('/proc/self/fd'))
            left.sendmsg([b'{"ready":true}'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array('i',self.descriptors))])
            with self.assertRaises(ControlError):channel.receive_packet(right)
            self.assertEqual(set(os.listdir('/proc/self/fd')),before)


if __name__=='__main__':unittest.main()
