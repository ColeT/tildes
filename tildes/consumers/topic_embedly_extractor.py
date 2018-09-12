# Copyright (c) 2018 Tildes contributors <code@tildes.net>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Consumer that fetches data from Embedly's Extract API for link topics."""

from datetime import timedelta
import os
from typing import Sequence

from amqpy import Message
from pyramid.paster import bootstrap
from requests.exceptions import HTTPError
from sqlalchemy import cast, desc, func
from sqlalchemy.dialects.postgresql import JSONB

from tildes.enums import ScraperType
from tildes.lib.amqp import PgsqlQueueConsumer
from tildes.lib.datetime import utc_now
from tildes.models.scraper import ScraperResult
from tildes.models.topic import Topic
from tildes.scrapers import EmbedlyScraper


# don't rescrape the same url inside this time period
RESCRAPE_DELAY = timedelta(hours=24)


class TopicEmbedlyExtractor(PgsqlQueueConsumer):
    """Consumer that fetches data from Embedly's Extract API for link topics."""

    def __init__(
        self, api_key: str, queue_name: str, routing_keys: Sequence[str]
    ) -> None:
        """Initialize the consumer, including creating a scraper instance."""
        super().__init__(queue_name, routing_keys)

        self.scraper = EmbedlyScraper(api_key)

    def run(self, msg: Message) -> None:
        """Process a delivered message."""
        topic = (
            self.db_session.query(Topic).filter_by(topic_id=msg.body["topic_id"]).one()
        )

        if not topic.is_link_type:
            return

        # see if we already have a recent scrape result from the same url
        result = (
            self.db_session.query(ScraperResult)
            .filter(
                ScraperResult.url == topic.link,
                ScraperResult.scraper_type == ScraperType.EMBEDLY,
                ScraperResult.scrape_time > utc_now() - RESCRAPE_DELAY,
            )
            .order_by(desc(ScraperResult.scrape_time))
            .first()
        )

        # if not, scrape the url and store the result
        if not result:
            try:
                result = self.scraper.scrape_url(topic.link)
            except HTTPError:
                return

            self.db_session.add(result)

        # update the topic's link if embedly says the final url is different
        if topic.link != result.data["url"]:
            topic.link = result.data["url"]

        new_metadata = EmbedlyScraper.get_metadata_from_result(result)

        if new_metadata:
            # update the topic's content_metadata in a way that won't wipe out any
            # existing values, and can handle the column being null
            (
                self.db_session.query(Topic)
                .filter(Topic.topic_id == topic.topic_id)
                .update(
                    {
                        "content_metadata": func.coalesce(
                            Topic.content_metadata, cast({}, JSONB)
                        ).op("||")(new_metadata)
                    },
                    synchronize_session=False,
                )
            )


if __name__ == "__main__":
    # pylint: disable=invalid-name
    env = bootstrap(os.environ["INI_FILE"])
    embedly_api_key = env["registry"].settings.get("api_keys.embedly")
    if not embedly_api_key:
        raise RuntimeError("No embedly API key available in INI file")

    TopicEmbedlyExtractor(
        embedly_api_key,
        queue_name="topic_embedly_extractor.q",
        routing_keys=["topic.created"],
    ).consume_queue()
