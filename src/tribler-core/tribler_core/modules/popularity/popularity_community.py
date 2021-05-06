import heapq
import random
from binascii import unhexlify

from ipv8.lazy_community import lazy_wrapper

from pony.orm import db_session

from tribler_core.modules.metadata_store.community.remote_query_community import RemoteQueryCommunity
from tribler_core.modules.popularity.payload import TorrentsHealthPayload
from tribler_core.utilities.unicode import hexlify


class PopularityCommunity(RemoteQueryCommunity):
    """
    Community for disseminating the content across the network.

    Every 5 seconds it gossips 5 the most popular torrents and 5 random torrents to
    a random peer.

    Gossiping is for checked torrents only.
    """
    GOSSIP_INTERVAL = 5
    GOSSIP_POPULAR_TORRENT_COUNT = 5
    GOSSIP_RANDOM_TORRENT_COUNT = 5

    community_id = unhexlify('9aca62f878969c437da9844cba29a134917e1648')

    def __init__(self, *args, **kwargs):
        self.torrent_checker = kwargs.pop('torrent_checker', None)

        super().__init__(*args, **kwargs)

        self.add_message_handler(TorrentsHealthPayload, self.on_torrents_health)

        self.logger.info('Popularity Community initialized (peer mid %s)',
                         hexlify(self.my_peer.mid))
        self.register_task("gossip", self.gossip_torrents_health,
                           interval=PopularityCommunity.GOSSIP_INTERVAL)

    @staticmethod
    def select_torrents_to_gossip(torrents) -> (set, set):
        """ Select torrents to gossip.

        Select top 5 popular torrents, and 5 random torrents.

        Args:
            torrents: set of tuples (infohash, seeders, leechers, last_check)

        Returns:
            tuple (set(popular), set(random))

        """
        # select the torrents that have seeders
        alive = {(_, seeders, *rest) for (_, seeders, *rest) in torrents
                    if seeders > 0}
        if not alive:
            return {}, {}

        # select 5 most popular from alive torrents, using `seeders` as a key
        count = PopularityCommunity.GOSSIP_POPULAR_TORRENT_COUNT
        popular = set(heapq.nlargest(count, alive, key=lambda t: t[1]))

        # select 5 random torrents from the rest of the list
        rest = alive - popular
        count = min(PopularityCommunity.GOSSIP_RANDOM_TORRENT_COUNT, len(rest))
        rand = set(random.sample(rest, count))

        return popular, rand

    def gossip_torrents_health(self):
        """
        Gossip torrent health information to another peer.
        """
        if not self.get_peers() or not self.torrent_checker:
            return

        checked = self.torrent_checker.torrents_checked
        if not checked:
            return

        popular, rand = PopularityCommunity.select_torrents_to_gossip(checked)
        if not popular and not rand:
            self.logger.info(f'No torrents to gossip. Checked torrents count: '
                             f'{len(checked)}')
            return

        random_peer = random.choice(self.get_peers())

        self.logger.info(
            f'Gossip torrent health information for {len(rand)}'
            f' random torrents and {len(popular)} popular torrents')

        self.ez_send(random_peer, TorrentsHealthPayload.create(rand, popular))

    @lazy_wrapper(TorrentsHealthPayload)
    async def on_torrents_health(self, peer, payload):
        self.logger.info(f"Received torrent health information for "
                         f"{len(payload.torrents_checked)} popular torrents and"
                         f" {len(payload.random_torrents)} random torrents")

        torrents = payload.random_torrents + payload.torrents_checked

        for infohash in await self.mds.run_threaded(self.process_torrents_health, torrents):
            # Get a single result per infohash to avoid duplicates
            self.send_remote_select(peer=peer, infohash=infohash, last=1)

    @db_session
    def process_torrents_health(self, torrent_healths):
        infohashes_to_resolve = set()
        for infohash, seeders, leechers, last_check in torrent_healths:
            added = self.mds.process_torrent_health(infohash, seeders, leechers, last_check)
            if added:
                infohashes_to_resolve.add(infohash)
        return infohashes_to_resolve
