import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from asgiref.sync import sync_to_async
from .models import Game
from django.contrib.auth import get_user_model
from django.db import models
from django.db import transaction
from django.db.models import Q
import asyncio
import logging
import time
from datetime import timedelta

logger = logging.getLogger(__name__)
User = get_user_model()

class GameConsumer(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.game = None
        self.channel_group_name = None
        self.user_group = None
        self.is_connected = False
        self.last_paddle_update = {}
        self.paddle_update_interval = 0.008  # 8ms for smoother movement
        self.messageCount = 0
        self.lastLogTime = 0

    @database_sync_to_async
    def create_game_sync(self):
        """Create a new game in the database"""
        try:
            game = Game.objects.create(player1=self.user)
            logger.info(f"Game {game.id} created by {self.user.username}")
            return game
        except Exception as e:
            logger.error(f"Error creating game: {str(e)}", exc_info=True)
            return None

    async def create_game(self):
        """Create a new game and join it immediately"""
        try:
            # Create the game in database
            game = await self.create_game_sync()
            if not game:
                await self.send_json({
                    'type': 'error',
                    'message': 'Failed to create game'
                })
                return

            # Set up channel group for this game
            self.game = game
            self.channel_group_name = f"game_{game.id}"
            await self.channel_layer.group_add(
                self.channel_group_name,
                self.channel_name
            )
            logger.info(f"[GAME {game.id}] Player {self.user.username} added to channel group {self.channel_group_name}")

            # Send confirmation to creator
            await self.send_json({
                'type': 'game_created',
                'game_id': str(game.id),
                'player_id': self.user.id,
                'game_state': game.game_state
            })

            logger.info(f"Game {game.id} created and joined by {self.user.username}")
            return game

        except Exception as e:
            logger.error(f"Error in create_game: {str(e)}", exc_info=True)
            await self.send_json({
                'type': 'error',
                'message': 'Failed to create game'
            })
            return None

    @database_sync_to_async
    def update_game_state_sync(self, game_id, new_state, update_type='all'):
        """Update game state in database
        
        Args:
            game_id: ID of the game to update
            new_state: New game state
            update_type: Type of update ('all', 'paddle', 'ball')
        """
        try:
            game = Game.objects.get(id=game_id)
            current_state = game.game_state
            
            if update_type == 'paddle':
                # Only update paddle positions
                if 'paddles' in new_state:
                    current_state['paddles'] = new_state['paddles']
                    logger.info(f"[GAME {game_id}] Updated paddle positions")
            else:
                # Update everything
                current_state = new_state
            
            game.game_state = current_state
            game.save(update_fields=['game_state', 'updated_at'])
            return True
            
        except Game.DoesNotExist:
            logger.error(f"Game {game_id} not found")
            return False
        except Exception as e:
            logger.error(f"Error updating game state: {str(e)}", exc_info=True)
            return False

    async def join_game(self):
        """Join an existing game"""
        try:
            # First, try to find an available game
            game = await self.find_available_game()
            if not game:
                logger.info("No available games found")
                return None

            if game.status == 'waiting' and game.player1 != self.user:
                # Join as player 2
                game.player2 = self.user
                game.status = 'active'
                await sync_to_async(game.save)()

                # Set up channel group
                self.game = game  # Set self.game here
                self.channel_group_name = f"game_{game.id}"
                await self.channel_layer.group_add(
                    self.channel_group_name,
                    self.channel_name
                )
                logger.info(f"[GAME {game.id}] Player {self.user.username} added to channel group {self.channel_group_name}")

                # Broadcast join message with game state from database
                await self.channel_layer.group_send(
                    self.channel_group_name,
                    {
                        'type': 'game_joined',
                        'game_id': str(game.id),
                        'player1_id': game.player1.id,
                        'player2_id': self.user.id,
                        'game_state': game.game_state
                    }
                )
                logger.info(f"[GAME {game.id}] Broadcasted join message to group {self.channel_group_name}")
                return game

            elif game.status == 'active' and (game.player1 == self.user or game.player2 == self.user):
                # Reconnecting to an active game
                self.game = game  # Set self.game here
                self.channel_group_name = f"game_{game.id}"
                await self.channel_layer.group_add(
                    self.channel_group_name,
                    self.channel_name
                )
                logger.info(f"[GAME {game.id}] Player {self.user.username} reconnected to channel group {self.channel_group_name}")
                
                # Send current game state
                await self.send_json({
                    'type': 'game_state_update',
                    'game_state': game.game_state
                })
                return game

            else:
                await self.send_json({
                    'type': 'error',
                    'message': 'Game is not available for joining'
                })
                return None

        except Exception as e:
            logger.error(f"Error joining game: {str(e)}", exc_info=True)
            return None

    async def paddle_move(self, direction, game_id):
        """Handle paddle movement"""
        try:
            logger.info(f"[GAME {game_id}] Received paddle_move: direction={direction}")
            
            game = await self.get_game(game_id)
            if not game:
                logger.error(f"[GAME {game_id}] Game not found")
                return
            if game.status != 'active':
                logger.error(f"[GAME {game_id}] Game not active")
                return

            # Determine which paddle to move
            if self.user.id == game.player1.id:
                paddle_key = 'player1'
            elif self.user.id == game.player2.id:
                paddle_key = 'player2'
            else:
                logger.error(f"[GAME {game_id}] User {self.user.username} is not a player")
                return
                
            logger.info(f"[GAME {game_id}] Moving {paddle_key}'s paddle")
            
            # Rate limiting
            current_time = time.time()
            last_update = self.last_paddle_update.get(paddle_key, 0)
            if current_time - last_update < self.paddle_update_interval:
                logger.debug(f"[GAME {game_id}] Rate limiting paddle movement")
                return
            self.last_paddle_update[paddle_key] = current_time

            # Update paddle position in game state
            game_state = game.game_state
            paddle = game_state['paddles'][paddle_key]
            paddle_speed = game_state['paddle_speed']
            canvas_height = game_state['canvas']['height']
            paddle_height = paddle['height']

            current_y = paddle['y']
            if direction == 'up':
                new_y = max(0, current_y - paddle_speed)
            elif direction == 'down':
                new_y = min(canvas_height - paddle_height, current_y + paddle_speed)
            else:
                logger.error(f"[GAME {game_id}] Invalid direction: {direction}")
                return

            # Update position in game state
            game_state['paddles'][paddle_key]['y'] = new_y
            
            # Save to database with paddle update type
            await self.update_game_state_sync(game.id, game_state, update_type='paddle')

            logger.info(f"[GAME {game.id}] {self.user.username} moved {paddle_key} {direction}. New Y: {new_y}")

            # Broadcast to all players
            await self.channel_layer.group_send(
                self.channel_group_name,
                {
                    'type': 'game_state_update',
                    'game_state': game_state
                }
            )

        except Exception as e:
            logger.error(f"Error in paddle_move: {str(e)}", exc_info=True)

    async def update_ball_position(self):
        """Update ball position"""
        try:
            game = await self.get_game(self.game.id)
            if not game or game.status != 'active':
                return

            game_state = game.game_state
            ball = game_state['ball']

            # Update ball position
            ball['x'] += ball['dx']
            ball['y'] += ball['dy']

            # Save to database with ball update type
            await self.update_game_state_sync(game.id, game_state, update_type='ball')

            # Broadcast new state
            await self.channel_layer.group_send(
                self.channel_group_name,
                {
                    'type': 'game_state_update',
                    'game_state': game_state
                }
            )

        except Exception as e:
            logger.error(f"Error updating ball position: {str(e)}", exc_info=True)

    async def game_state_update(self, event):
        """Handle game state update"""
        try:
            game_state = event['game_state']
            logger.info(f"[GAME {self.game.id if self.game else 'Unknown'}] Sending game state update to client {self.user.username}")
            await self.send_json({
                'type': 'game_state_update',
                'game_state': game_state
            })
        except Exception as e:
            logger.error(f"Error in game_state_update: {str(e)}", exc_info=True)

    async def connect(self):
        try:
            if self.scope["user"].is_anonymous:
                await self.close()
                return

            self.user = self.scope["user"]
            self.user_id = self.user.id
            self.username = self.user.username
            self.is_connected = True
            self.game = None
            self.user_group = f"user_{self.user.id}"
            
            # Accept the connection first
            await self.accept()
            
            # Then send the message
            await self.send_json({
                'type': 'connection_established',
                'message': 'Connected successfully',
                'user': {
                    'id': self.user_id,
                    'username': self.username
                }
            })
            
            logger.info(f"User {self.user.username} connected successfully")

        except Exception as e:
            logger.error(f"Error in connect: {str(e)}", exc_info=True)
            await self.close()

    async def receive(self, text_data=None, bytes_data=None):
        try:
            if not text_data:
                return
                
            logger.info(f"Received text data from {self.user.username}: {text_data}")
            data = json.loads(text_data)
            message_type = data.get('type')
            
            # Handle heartbeat messages
            if message_type == 'heartbeat':
                await self.send_json({
                    'type': 'heartbeat_response'
                })
                return
            
            # Validate user session
            if self.scope["user"].is_anonymous:
                await self.send_json({
                    'type': 'error',
                    'message': 'Invalid session'
                })
                await self.close()
                return

            self.messageCount += 1
            current_time = time.time()
            
            # Log every 100th message or if more than 5 seconds have passed
            if self.messageCount % 100 == 0 or current_time - self.lastLogTime > 5:
                logger.info(f"Received message {self.messageCount}: type={message_type}")
                self.lastLogTime = current_time

            if message_type == 'create_game':
                await self.create_game()
            
            elif message_type == 'join_game':
                game_id = data.get('game_id')
                await self.join_game()
            
            elif message_type == 'paddle_move':
                game_id = data.get('game_id')
                direction = data.get('direction')
                logger.info(f"Received paddle_move message: game_id={game_id}, direction={direction}")
                if not game_id:
                    logger.error("Missing game_id in paddle_move message")
                    return
                if not direction:
                    logger.error("Missing direction in paddle_move message")
                    return
                await self.paddle_move(direction, game_id)
            
            elif message_type == 'user_connected':
                # Handle user connection info
                user_id = data.get('user_id')
                username = data.get('username')
                
                # Validate that the WebSocket user matches the session user
                if str(self.user_id) != str(user_id) or self.username != username:
                    await self.send_json({
                        'type': 'error',
                        'message': 'User session mismatch'
                    })
                    await self.close()
                    return
                
                logger.info(f"User connected: {username} (ID: {user_id})")

            else:
                await self.send_json({
                    'type': 'error',
                    'message': f'Unknown message type: {message_type}'
                })
                
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON received: {text_data}")
            await self.send_json({
                'type': 'error',
                'message': 'Invalid JSON message'
            })
        except Exception as e:
            logger.error(f"Error in receive: {str(e)}", exc_info=True)
            await self.send_json({
                'type': 'error',
                'message': 'Internal server error'
            })

    @database_sync_to_async
    def get_game_with_players(self, game_id):
        """Get game with player info from database"""
        try:
            game = Game.objects.select_related('player1', 'player2').get(id=game_id)
            return game
        except Game.DoesNotExist:
            logger.error(f"Game {game_id} not found")
            return None
        except Exception as e:
            logger.error(f"Error getting game: {str(e)}", exc_info=True)
            return None

    @database_sync_to_async
    def update_game_player2(self, game, player2):
        """Update game's player2 and status"""
        try:
            game.player2 = player2
            game.status = 'active'
            game.save(update_fields=['player2', 'status', 'updated_at'])
            return game
        except Exception as e:
            logger.error(f"Error updating game: {str(e)}", exc_info=True)
            return None

    @database_sync_to_async
    def get_game(self, game_id):
        """Get game by id with player information"""
        try:
            return Game.objects.select_related('player1', 'player2').get(id=game_id)
        except Game.DoesNotExist:
            return None
        except Exception as e:
            logger.error(f"Error getting game: {str(e)}", exc_info=True)
            return None

    @database_sync_to_async
    def find_available_game(self):
        """Find an available game to join"""
        try:
            # Look for a game waiting for players
            game = Game.objects.select_related('player1', 'player2').filter(
                status='waiting'
            ).first()
            
            if game:
                logger.info(f"Found available game {game.id}")
            return game
            
        except Exception as e:
            logger.error(f"Error finding available game: {str(e)}", exc_info=True)
            return None

    async def game_joined(self, event):
        try:
            # Initialize game state
            if not hasattr(self, '_game_state'):
                self._game_state = event['game_state']
            
            await self.send_json({
                'type': 'game_joined',
                'game_id': event['game_id'],
                'player1_id': event['player1_id'],
                'player2_id': event['player2_id'],
                'game_state': self._game_state
            })
            
            # Start game loop when both players have joined
            if self.game and self.game.status == 'active' and self.game.player1 and self.game.player2:
                asyncio.create_task(self.game_loop())
        except Exception as e:
            logger.error(f'Error in game_joined: {str(e)}', exc_info=True)

    async def game_loop(self):
        """Game loop to update ball position"""
        try:
            logger.info(f"[GAME {self.game.id}] Starting game loop")
            
            while self.game and self.game.status == 'active':
                # Get current game state
                game = await self.get_game(self.game.id)
                if not game:
                    break
                
                # Create a copy of the game state to avoid modifying it directly
                game_state = game.game_state.copy()
                ball = game_state['ball']
                
                # Update ball position
                ball['x'] += ball['dx']
                ball['y'] += ball['dy']
                
                # Ball collision with top and bottom walls
                if ball['y'] <= ball['radius'] or ball['y'] >= game_state['canvas']['height'] - ball['radius']:
                    ball['dy'] *= -1
                
                # Ball collision with paddles
                paddles = game_state['paddles']
                
                # Left paddle collision
                if (ball['x'] - ball['radius'] <= paddles['player1']['x'] + paddles['player1']['width'] and
                    ball['y'] >= paddles['player1']['y'] and
                    ball['y'] <= paddles['player1']['y'] + paddles['player1']['height']):
                    ball['dx'] = abs(ball['dx'])  # Ensure ball moves right
                    ball['dx'] *= 1.1  # Speed up slightly
                
                # Right paddle collision
                if (ball['x'] + ball['radius'] >= paddles['player2']['x'] and
                    ball['y'] >= paddles['player2']['y'] and
                    ball['y'] <= paddles['player2']['y'] + paddles['player2']['height']):
                    ball['dx'] = -abs(ball['dx'])  # Ensure ball moves left
                    ball['dx'] *= 1.1  # Speed up slightly
                
                # Ball out of bounds - scoring
                if ball['x'] < 0:  # Player 2 scores
                    game_state['score']['player2'] += 1
                    ball['x'] = game_state['canvas']['width'] / 2
                    ball['y'] = game_state['canvas']['height'] / 2
                    ball['dx'] = -5  # Reset speed and direction
                    ball['dy'] = 5 if ball['dy'] > 0 else -5
                elif ball['x'] > game_state['canvas']['width']:  # Player 1 scores
                    game_state['score']['player1'] += 1
                    ball['x'] = game_state['canvas']['width'] / 2
                    ball['y'] = game_state['canvas']['height'] / 2
                    ball['dx'] = 5  # Reset speed and direction
                    ball['dy'] = 5 if ball['dy'] > 0 else -5
                
                # Save to database
                await self.update_game_state_sync(self.game.id, game_state)

                # Send game state update to all players
                await self.channel_layer.group_send(
                    self.channel_group_name,
                    {
                        'type': 'game_state_update',
                        'game_state': game_state
                    }
                )
                
                # Wait before next update (60 FPS)
                await asyncio.sleep(1/60)
                
        except Exception as e:
            logger.error(f"Error in game loop: {str(e)}", exc_info=True)
            await self.send_json({
                'type': 'error',
                'message': 'Game loop error'
            })

    async def send_json(self, content):
        """Send JSON message to WebSocket"""
        try:
            self.messageCount += 1
            logInterval = 50
            currentTime = time.time()
            if self.messageCount % logInterval == 0:
                self.lastLogTime = currentTime
                logger.debug(f"Sending message to {self.user.username}: {content}")
            await self.send(text_data=json.dumps(content))
        except Exception as e:
            logger.error(f"Error sending message: {str(e)}", exc_info=True)

    async def disconnect(self, close_code):
        """Handle disconnect"""
        try:
            if hasattr(self, 'channel_group_name'):
                await self.channel_layer.group_discard(
                    self.channel_group_name,
                    self.channel_name
                )
        except Exception as e:
            logger.error(f"Error in disconnect: {str(e)}", exc_info=True)
        finally:
            logger.info(f"User {self.user.username if hasattr(self, 'user') else 'Unknown'} disconnected")
