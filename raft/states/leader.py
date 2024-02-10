from concurrent.futures import ThreadPoolExecutor
import pickle
import rpyc

from raft.utils.models import AppendEntry, RequestVote, AppendEntryResponse
from .state import State


class Leader(State):
    """
    Represents the Leader state in the Raft consensus algorithm.
    Inherits from the State base class.
    """
    def __init__(self, node) -> None:
        """
        Initializes a Leader instance.

        Args:
            node: The Raft node associated with this state.
        """
        super().__init__(node, node.cluster.heartbeat_interval, node.cluster.heartbeat_interval)
        # Initialize next and match index for each peer
        self.next_idx = {peer.id: len(self._node.data.logs.logs) for peer in self._node.peers.values()}
        self.match_idx = {peer.id: 0 for peer in self._node.peers.values()}
        # Set current leader information
        self._node.current_leader["id"] = self._node.id

    def on_expire(self):
        """
        Callback method triggered upon state transition timeout.
        Sends heartbeat AppendEntries RPCs to all peers.
        """
        # Reset timer
        self._timer.reset()
        # Send AppendEntries RPCs to all peers
        self.broadcast_rpc()

    def on_append_entry(self, ae: AppendEntry):
        """
        Handles incoming AppendEntry RPC messages.

        Args:
            ae (AppendEntry): The AppendEntry message.

        Returns:
            bool: False if the message is rejected, otherwise delegates to follower's on_append_entry method.
        """
        # Leader becomes follower if it receives a valid AppendEntry message with higher term
        if self._node.data.current_term >= ae.term:
            return False
        self.become_follower(ae.term)
        return self._node.state.on_append_entry(ae)

    def on_append_entry_callback(self, res: AppendEntryResponse):
        """
        Handles responses to AppendEntry RPC messages.

        Args:
            res (AppendEntryResponse): The AppendEntry response.
        """
        if res.acknowledge is False:
            # Decrement next index for the corresponding peer if the AppendEntry was rejected
            if self._node.data.current_term < res.term:
                return self.become_follower(res.term)
            else:
                self.next_idx[res.id] -= 1
        else:
            # Update next and match index for the corresponding peer
            self.next_idx[res.id] = res.acknowledge + 1
            self.match_idx[res.id] = max(res.acknowledge, self.match_idx[res.id])
            # Calculate new commit index, it is the low-median of the match_idx list
            sorted_match_index = sorted(self.match_idx.values())
            self._node.commit_index = max(
                self._node.commit_index,
                sorted_match_index[len(sorted_match_index)//2]
            )
            # Apply committed commands to the state machine
            self._node.apply_commands()

    def on_request_vote(self, rv: RequestVote):
        if self._node.data.current_term >= rv.term:
            return False
        self.become_follower(rv.term)
        return True

    def call_rpc(self, peer_id, peer_addr):
        try:
            rpyc.connect(*peer_addr).root.append_entry(
                pickle.dumps(AppendEntry(
                    term=self._node.data.current_term,
                    leader=self._node.id,
                    previous_log_index=self.next_idx[peer_id] - 1,
                    previous_log_term=self._node.data.logs.logs[self.next_idx[peer_id] - 1].term,
                    leader_commit=self._node.commit_index,
                    entries=self._node.data.logs.logs[self.next_idx[peer_id]:]
                )),
                self._node.append_entry_callback
            )
        except:
            return