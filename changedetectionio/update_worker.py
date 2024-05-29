import os
import threading
import queue
import time
from . import content_fetchers
from changedetectionio import html_tools
from .processors.text_json_diff import FilterNotFoundInResponse
from .processors.restock_diff import UnableToExtractRestockData

# A single update worker
#
# Requests for checking on a single site(watch) from a queue of watches
# (another process inserts watches into the queue that are time-ready for checking)

import sys
from loguru import logger

class update_worker(threading.Thread):
    current_uuid = None

    def __init__(self, q, notification_q, app, datastore, *args, **kwargs):
        self.q = q
        self.app = app
        self.notification_q = notification_q
        self.datastore = datastore
        super().__init__(*args, **kwargs)

    def queue_notification_for_watch(self, notification_q, n_object, watch):

        from changedetectionio import diff
        dates = []
        trigger_text = ''

        now = time.time()

        if watch:
            watch_history = watch.history
            dates = list(watch_history.keys())
            trigger_text = watch.get('trigger_text', [])

        # Add text that was triggered
        if len(dates):
            snapshot_contents = watch.get_history_snapshot(dates[-1])
        else:
            snapshot_contents = "No snapshot/history available, the watch should fetch atleast once."

        # HTML needs linebreak, but MarkDown and Text can use a linefeed
        if n_object.get('notification_format') == 'HTML':
            line_feed_sep = "<br>"
            # Snapshot will be plaintext on the disk, convert to some kind of HTML
            snapshot_contents = snapshot_contents.replace('\n', line_feed_sep)
        else:
            line_feed_sep = "\n"

        triggered_text = ''
        if len(trigger_text):
            from . import html_tools
            triggered_text = html_tools.get_triggered_text(content=snapshot_contents, trigger_text=trigger_text)
            if triggered_text:
                triggered_text = line_feed_sep.join(triggered_text)

        # Could be called as a 'test notification' with only 1 snapshot available
        prev_snapshot = "Example text: example test\nExample text: change detection is cool\nExample text: some more examples\n"
        current_snapshot = "Example text: example test\nExample text: change detection is fantastic\nExample text: even more examples\nExample text: a lot more examples"

        if len(dates) > 1:
            prev_snapshot = watch.get_history_snapshot(dates[-2])
            current_snapshot = watch.get_history_snapshot(dates[-1])

        n_object.update({
            'current_snapshot': snapshot_contents,
            'diff': diff.render_diff(prev_snapshot, current_snapshot, line_feed_sep=line_feed_sep),
            'diff_added': diff.render_diff(prev_snapshot, current_snapshot, include_removed=False, line_feed_sep=line_feed_sep),
            'diff_full': diff.render_diff(prev_snapshot, current_snapshot, include_equal=True, line_feed_sep=line_feed_sep),
            'diff_patch': diff.render_diff(prev_snapshot, current_snapshot, line_feed_sep=line_feed_sep, patch_format=True),
            'diff_removed': diff.render_diff(prev_snapshot, current_snapshot, include_added=False, line_feed_sep=line_feed_sep),
            'notification_timestamp': now,
            'screenshot': watch.get_screenshot() if watch and watch.get('notification_screenshot') else None,
            'triggered_text': triggered_text,
            'uuid': watch.get('uuid') if watch else None,
            'watch_url': watch.get('url') if watch else None,
        })
        logger.trace(f"Main rendered notification placeholders (diff_added etc) calculated in {time.time()-now:.3f}s")
        logger.debug("Queued notification for sending")
        notification_q.put(n_object)

    # Prefer - Individual watch settings > Tag settings >  Global settings (in that order)
    def _check_cascading_vars(self, var_name, watch):

        from changedetectionio.notification import (
            default_notification_format_for_watch,
            default_notification_body,
            default_notification_title
        )


        # Would be better if this was some kind of Object where Watch can reference the parent datastore etc
        v = watch.get(var_name)
        if v and not watch.get('notification_muted'):
            if var_name == 'notification_format' and v == default_notification_format_for_watch:
                return self.datastore.data['settings']['application'].get('notification_format')

            return v

        tags = self.datastore.get_all_tags_for_watch(uuid=watch.get('uuid'))
        if tags:
            for tag_uuid, tag in tags.items():
                v = tag.get(var_name)
                if v and not tag.get('notification_muted'):
                    return v

        if self.datastore.data['settings']['application'].get(var_name):
            return self.datastore.data['settings']['application'].get(var_name)

        # Otherwise could be defaults
        if var_name == 'notification_format':
            return default_notification_format_for_watch
        if var_name == 'notification_body':
            return default_notification_body
        if var_name == 'notification_title':
            return default_notification_title

        return None

    def send_content_changed_notification(self, watch_uuid):

        n_object = {}
        watch = self.datastore.data['watching'].get(watch_uuid)
        if not watch:
            return

        watch_history = watch.history
        dates = list(watch_history.keys())
        # Theoretically it's possible that this could be just 1 long,
        # - In the case that the timestamp key was not unique
        if len(dates) == 1:
            raise ValueError(
                "History index had 2 or more, but only 1 date loaded, timestamps were not unique? maybe two of the same timestamps got written, needs more delay?"
            )

        # Should be a better parent getter in the model object

        # Prefer - Individual watch settings > Tag settings >  Global settings (in that order)
        n_object['notification_urls'] = self._check_cascading_vars('notification_urls', watch)
        n_object['notification_title'] = self._check_cascading_vars('notification_title', watch)
        n_object['notification_body'] = self._check_cascading_vars('notification_body', watch)
        n_object['notification_format'] = self._check_cascading_vars('notification_format', watch)

        # (Individual watch) Only prepare to notify if the rules above matched
        queued = False
        if n_object and n_object.get('notification_urls'):
            queued = True

            count = watch.get('notification_alert_count', 0) + 1
            self.datastore.update_watch(uuid=watch_uuid, update_obj={'notification_alert_count': count})

            self.queue_notification_for_watch(notification_q=self.notification_q, n_object=n_object, watch=watch)

        return queued


    def send_filter_failure_notification(self, watch_uuid):

        threshold = self.datastore.data['settings']['application'].get('filter_failure_notification_threshold_attempts')
        watch = self.datastore.data['watching'].get(watch_uuid)
        if not watch:
            return

        n_object = {'notification_title': 'Changedetection.io - Alert - CSS/xPath filter was not present in the page',
                    'notification_body': "Your configured CSS/xPath filters of '{}' for {{{{watch_url}}}} did not appear on the page after {} attempts, did the page change layout?\n\nLink: {{{{base_url}}}}/edit/{{{{watch_uuid}}}}\n\nThanks - Your omniscient vastarm.com installation :)\n".format(
                        ", ".join(watch['include_filters']),
                        threshold),
                    'notification_format': 'text'}

        if len(watch['notification_urls']):
            n_object['notification_urls'] = watch['notification_urls']

        elif len(self.datastore.data['settings']['application']['notification_urls']):
            n_object['notification_urls'] = self.datastore.data['settings']['application']['notification_urls']

        # Only prepare to notify if the rules above matched
        if 'notification_urls' in n_object:
            n_object.update({
                'watch_url': watch['url'],
                'uuid': watch_uuid,
                'screenshot': None
            })
            self.notification_q.put(n_object)
            logger.error(f"Sent filter not found notification for {watch_uuid}")

    def send_step_failure_notification(self, watch_uuid, step_n):
        watch = self.datastore.data['watching'].get(watch_uuid, False)
        if not watch:
            return
        threshold = self.datastore.data['settings']['application'].get('filter_failure_notification_threshold_attempts')
        n_object = {'notification_title': "Changedetection.io - Alert - Browser step at position {} could not be run".format(step_n+1),
                    'notification_body': "Your configured browser step at position {} for {{{{watch_url}}}} "
                                         "did not appear on the page after {} attempts, did the page change layout? "
                                         "Does it need a delay added?\n\nLink: {{{{base_url}}}}/edit/{{{{watch_uuid}}}}\n\n"
                                         "Thanks - Your omniscient vastarm.com installation :)\n".format(step_n+1, threshold),
                    'notification_format': 'text'}

        if len(watch['notification_urls']):
            n_object['notification_urls'] = watch['notification_urls']

        elif len(self.datastore.data['settings']['application']['notification_urls']):
            n_object['notification_urls'] = self.datastore.data['settings']['application']['notification_urls']

        # Only prepare to notify if the rules above matched
        if 'notification_urls' in n_object:
            n_object.update({
                'watch_url': watch['url'],
                'uuid': watch_uuid
            })
            self.notification_q.put(n_object)
            logger.error(f"Sent step not found notification for {watch_uuid}")


    def cleanup_error_artifacts(self, uuid):
        # All went fine, remove error artifacts
        cleanup_files = ["last-error-screenshot.png", "last-error.txt"]
        for f in cleanup_files:
            full_path = os.path.join(self.datastore.datastore_path, uuid, f)
            if os.path.isfile(full_path):
                os.unlink(full_path)

    def run(self):

        from .processors import text_json_diff, restock_diff
        now = time.time()
        
        while not self.app.config.exit.is_set():
            update_handler = None

            try:
                queued_item_data = self.q.get(block=False)
            except queue.Empty:
                pass

            else:
                uuid = queued_item_data.item.get('uuid')
                self.current_uuid = uuid
                if uuid in list(self.datastore.data['watching'].keys()) and self.datastore.data['watching'][uuid].get('url'):
                    changed_detected = False
                    contents = b''
                    process_changedetection_results = True
                    update_obj = {}
                    logger.info(f"Processing watch UUID {uuid} "
                            f"Priority {queued_item_data.priority} "
                            f"URL {self.datastore.data['watching'][uuid]['url']}")
                    now = time.time()

                    try:
                        # Processor is what we are using for detecting the "Change"
                        processor = self.datastore.data['watching'][uuid].get('processor', 'text_json_diff')
                        # if system...

                        # Abort processing when the content was the same as the last fetch
                        skip_when_same_checksum = queued_item_data.item.get('skip_when_checksum_same')


                        # @todo some way to switch by name
                        # Init a new 'difference_detection_processor'

                        if processor == 'restock_diff':
                            update_handler = restock_diff.perform_site_check(datastore=self.datastore,
                                                                             watch_uuid=uuid
                                                                             )
                        else:
                            # Used as a default and also by some tests
                            update_handler = text_json_diff.perform_site_check(datastore=self.datastore,
                                                                               watch_uuid=uuid
                                                                               )

                        # Clear last errors (move to preflight func?)
                        self.datastore.data['watching'][uuid]['browser_steps_last_error_step'] = None

                        update_handler.call_browser()

                        changed_detected, update_obj, contents = update_handler.run_changedetection(uuid,
                                                                                    skip_when_checksum_same=skip_when_same_checksum,
                                                                                    )

                        # Re #342
                        # In Python 3, all strings are sequences of Unicode characters. There is a bytes type that holds raw bytes.
                        # We then convert/.decode('utf-8') for the notification etc
                        if not isinstance(contents, (bytes, bytearray)):
                            raise Exception("Error - returned data from the fetch handler SHOULD be bytes")
                    except PermissionError as e:
                        logger.critical(f"File permission error updating file, watch: {uuid}")
                        logger.critical(str(e))
                        process_changedetection_results = False
                    except content_fetchers.exceptions.ReplyWithContentButNoText as e:
                        # Totally fine, it's by choice - just continue on, nothing more to care about
                        # Page had elements/content but no renderable text
                        # Backend (not filters) gave zero output
                        extra_help = ""
                        if e.has_filters:
                            # Maybe it contains an image? offer a more helpful link
                            has_img = html_tools.include_filters(include_filters='img',
                                                                 html_content=e.html_content)
                            if has_img:
                                extra_help = ", it's possible that the filters you have give an empty result or contain only an image."
                            else:
                                extra_help = ", it's possible that the filters were found, but contained no usable text."

                        self.datastore.update_watch(uuid=uuid, update_obj={
                            'last_error': f"Got HTML content but no text found (With {e.status_code} reply code){extra_help}"
                        })

                        if e.screenshot:
                            self.datastore.save_screenshot(watch_uuid=uuid, screenshot=e.screenshot)
                        process_changedetection_results = False

                    except content_fetchers.exceptions.Non200ErrorCodeReceived as e:
                        if e.status_code == 403:
                            err_text = "Error - 403 (Access denied) received"
                        elif e.status_code == 404:
                            err_text = "Error - 404 (Page not found) received"
                        elif e.status_code == 407:
                            err_text = "Error - 407 (Proxy authentication required) received, did you need a username and password for the proxy?"
                        elif e.status_code == 500:
                            err_text = "Error - 500 (Internal server error) received from the web site"
                        else:
                            err_text = "Error - Request returned a HTTP error code {}".format(str(e.status_code))

                        if e.screenshot:
                            self.datastore.save_screenshot(watch_uuid=uuid, screenshot=e.screenshot, as_error=True)
                        if e.xpath_data:
                            self.datastore.save_xpath_data(watch_uuid=uuid, data=e.xpath_data, as_error=True)
                        if e.page_text:
                            self.datastore.save_error_text(watch_uuid=uuid, contents=e.page_text)

                        self.datastore.update_watch(uuid=uuid, update_obj={'last_error': err_text})
                        process_changedetection_results = False

                    except FilterNotFoundInResponse as e:
                        if not self.datastore.data['watching'].get(uuid):
                            continue

                        err_text = "Warning, no filters were found, no change detection ran - Did the page change layout? update your Visual Filter if necessary."
                        self.datastore.update_watch(uuid=uuid, update_obj={'last_error': err_text})

                        # Only when enabled, send the notification
                        if self.datastore.data['watching'][uuid].get('filter_failure_notification_send', False):
                            c = self.datastore.data['watching'][uuid].get('consecutive_filter_failures', 5)
                            c += 1
                            # Send notification if we reached the threshold?
                            threshold = self.datastore.data['settings']['application'].get('filter_failure_notification_threshold_attempts',
                                                                                           0)
                            logger.error(f"Filter for {uuid} not found, consecutive_filter_failures: {c}")
                            if threshold > 0 and c >= threshold:
                                if not self.datastore.data['watching'][uuid].get('notification_muted'):
                                    self.send_filter_failure_notification(uuid)
                                c = 0

                            self.datastore.update_watch(uuid=uuid, update_obj={'consecutive_filter_failures': c})

                        process_changedetection_results = False

                    except content_fetchers.exceptions.checksumFromPreviousCheckWasTheSame as e:
                        # Yes fine, so nothing todo, don't continue to process.
                        process_changedetection_results = False
                        changed_detected = False
                        self.datastore.update_watch(uuid=uuid, update_obj={'last_error': False})
                    except content_fetchers.exceptions.BrowserConnectError as e:
                        self.datastore.update_watch(uuid=uuid,
                                                    update_obj={'last_error': e.msg
                                                                }
                                                    )
                        process_changedetection_results = False
                    except content_fetchers.exceptions.BrowserFetchTimedOut as e:
                        self.datastore.update_watch(uuid=uuid,
                                                    update_obj={'last_error': e.msg
                                                                }
                                                    )
                        process_changedetection_results = False
                    except content_fetchers.exceptions.BrowserStepsStepException as e:

                        if not self.datastore.data['watching'].get(uuid):
                            continue

                        error_step = e.step_n + 1
                        from playwright._impl._errors import TimeoutError, Error

                        # Generally enough info for TimeoutError (couldnt locate the element after default seconds)
                        err_text = f"Browser step at position {error_step} could not run, check the watch, add a delay if necessary, view Browser Steps to see screenshot at that step."

                        if e.original_e.name == "TimeoutError":
                            # Just the first line is enough, the rest is the stack trace
                            err_text += " Could not find the target."
                        else:
                            # Other Error, more info is good.
                            err_text += " " + str(e.original_e).splitlines()[0]

                        logger.debug(f"BrowserSteps exception at step {error_step} {str(e.original_e)}")

                        self.datastore.update_watch(uuid=uuid,
                                                    update_obj={'last_error': err_text,
                                                                'browser_steps_last_error_step': error_step
                                                                }
                                                    )

                        if self.datastore.data['watching'][uuid].get('filter_failure_notification_send', False):
                            c = self.datastore.data['watching'][uuid].get('consecutive_filter_failures', 5)
                            c += 1
                            # Send notification if we reached the threshold?
                            threshold = self.datastore.data['settings']['application'].get('filter_failure_notification_threshold_attempts',
                                                                                           0)
                            logger.error(f"Step for {uuid} not found, consecutive_filter_failures: {c}")
                            if threshold > 0 and c >= threshold:
                                if not self.datastore.data['watching'][uuid].get('notification_muted'):
                                    self.send_step_failure_notification(watch_uuid=uuid, step_n=e.step_n)
                                c = 0

                            self.datastore.update_watch(uuid=uuid, update_obj={'consecutive_filter_failures': c})

                        process_changedetection_results = False

                    except content_fetchers.exceptions.EmptyReply as e:
                        # Some kind of custom to-str handler in the exception handler that does this?
                        err_text = "EmptyReply - try increasing 'Wait seconds before extracting text', Status Code {}".format(e.status_code)
                        self.datastore.update_watch(uuid=uuid, update_obj={'last_error': err_text,
                                                                           'last_check_status': e.status_code})
                        process_changedetection_results = False
                    except content_fetchers.exceptions.ScreenshotUnavailable as e:
                        err_text = "Screenshot unavailable, page did not render fully in the expected time or page was too long - try increasing 'Wait seconds before extracting text'"
                        self.datastore.update_watch(uuid=uuid, update_obj={'last_error': err_text,
                                                                           'last_check_status': e.status_code})
                        process_changedetection_results = False
                    except content_fetchers.exceptions.JSActionExceptions as e:
                        err_text = "Error running JS Actions - Page request - "+e.message
                        if e.screenshot:
                            self.datastore.save_screenshot(watch_uuid=uuid, screenshot=e.screenshot, as_error=True)
                        self.datastore.update_watch(uuid=uuid, update_obj={'last_error': err_text,
                                                                           'last_check_status': e.status_code})
                        process_changedetection_results = False
                    except content_fetchers.exceptions.PageUnloadable as e:
                        err_text = "Page request from server didnt respond correctly"
                        if e.message:
                            err_text = "{} - {}".format(err_text, e.message)

                        if e.screenshot:
                            self.datastore.save_screenshot(watch_uuid=uuid, screenshot=e.screenshot, as_error=True)

                        self.datastore.update_watch(uuid=uuid, update_obj={'last_error': err_text,
                                                                           'last_check_status': e.status_code,
                                                                           'has_ldjson_price_data': None})
                        process_changedetection_results = False
                    except content_fetchers.exceptions.BrowserStepsInUnsupportedFetcher as e:
                        err_text = "This watch has Browser Steps configured and so it cannot run with the 'Basic fast Plaintext/HTTP Client', either remove the Browser Steps or select a Chrome fetcher."
                        self.datastore.update_watch(uuid=uuid, update_obj={'last_error': err_text})
                        process_changedetection_results = False
                        logger.error(f"Exception (BrowserStepsInUnsupportedFetcher) reached processing watch UUID: {uuid}")

                    except UnableToExtractRestockData as e:
                        # Usually when fetcher.instock_data returns empty
                        logger.error(f"Exception (UnableToExtractRestockData) reached processing watch UUID: {uuid}")
                        logger.error(str(e))
                        self.datastore.update_watch(uuid=uuid, update_obj={'last_error': f"Unable to extract restock data for this page unfortunately. (Got code {e.status_code} from server)"})
                        process_changedetection_results = False
                    except Exception as e:
                        logger.error(f"Exception reached processing watch UUID: {uuid}")
                        logger.error(str(e))
                        self.datastore.update_watch(uuid=uuid, update_obj={'last_error': "Exception: " + str(e)})
                        # Other serious error
                        process_changedetection_results = False
#                        import traceback
#                        print(traceback.format_exc())

                    else:
                        # Crash protection, the watch entry could have been removed by this point (during a slow chrome fetch etc)
                        if not self.datastore.data['watching'].get(uuid):
                            continue

                        # Mark that we never had any failures
                        if not self.datastore.data['watching'][uuid].get('ignore_status_codes'):
                            update_obj['consecutive_filter_failures'] = 0

                        # Everything ran OK, clean off any previous error
                        update_obj['last_error'] = False

                        self.cleanup_error_artifacts(uuid)

                    #
                    # Different exceptions mean that we may or may not want to bump the snapshot, trigger notifications etc
                    if process_changedetection_results:
                        try:
                            watch = self.datastore.data['watching'].get(uuid)
                            self.datastore.update_watch(uuid=uuid, update_obj=update_obj)

                            # Also save the snapshot on the first time checked
                            if changed_detected or not watch['last_checked']:
                                watch.save_history_text(contents=contents,
                                                        timestamp=str(round(time.time())),
                                                        snapshot_id=update_obj.get('previous_md5', 'none'))

                            # A change was detected
                            if changed_detected:
                                # Notifications should only trigger on the second time (first time, we gather the initial snapshot)
                                if watch.history_n >= 2:
                                    logger.info(f"Change detected in UUID {uuid} - {watch['url']}")
                                    if not self.datastore.data['watching'][uuid].get('notification_muted'):
                                        self.send_content_changed_notification(watch_uuid=uuid)
                                else:
                                    logger.info(f"Change triggered in UUID {uuid} due to first history saving (no notifications sent) - {watch['url']}")

                        except Exception as e:
                            # Catch everything possible here, so that if a worker crashes, we don't lose it until restart!
                            logger.critical("!!!! Exception in update_worker while processing process_changedetection_results !!!")
                            logger.critical(str(e))
                            self.datastore.update_watch(uuid=uuid, update_obj={'last_error': str(e)})

                    if self.datastore.data['watching'].get(uuid):
                        # Always record that we atleast tried
                        count = self.datastore.data['watching'][uuid].get('check_count', 0) + 1

                        # Record the 'server' header reply, can be used for actions in the future like cloudflare/akamai workarounds
                        try:
                            server_header = update_handler.fetcher.headers.get('server', '').strip().lower()[:255]
                            self.datastore.update_watch(uuid=uuid,
                                                        update_obj={'remote_server_reply': server_header}
                                                        )
                        except Exception as e:
                            pass

                        self.datastore.update_watch(uuid=uuid, update_obj={'fetch_time': round(time.time() - now, 3),
                                                                           'last_checked': round(time.time()),
                                                                           'check_count': count
                                                                           })

                        # Always save the screenshot if it's available
                        if update_handler.screenshot:
                            self.datastore.save_screenshot(watch_uuid=uuid, screenshot=update_handler.screenshot)
                        if update_handler.xpath_data:
                            self.datastore.save_xpath_data(watch_uuid=uuid, data=update_handler.xpath_data)


                self.current_uuid = None  # Done
                self.q.task_done()
                logger.debug(f"Watch {uuid} done in {time.time()-now:.2f}s")

                # Give the CPU time to interrupt
                time.sleep(0.1)

            self.app.config.exit.wait(1)
