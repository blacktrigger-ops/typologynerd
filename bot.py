import os
import asyncio
import re
from datetime import datetime, timezone
from typing import List, Optional
import discord
from discord import ui, SelectOption
from discord.ext import commands
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import TEXT
from beanie import Document, init_beanie
from pydantic import Field
from dotenv import load_dotenv

load_dotenv()

# ======================
# DATABASE MODEL
# ======================
class TypologyEntry(Document):
    title: str
    category: str
    topic: str
    description: str
    author_id: int
    author_name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    image_url: str = Field(default="")
    reference: str = Field(default="")
    votes: int = Field(default=0)
    voters: List[int] = Field(default_factory=list)
    
    class Settings:
        name = "typology_entries"

# ======================
# DISTINCT VALUE HELPERS
# ======================
async def get_distinct_categories() -> List[str]:
    client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
    db = client[os.getenv("MONGO_DB", "typology_bot")]
    collection = db["typology_entries"]
    return await collection.distinct("category")

async def get_distinct_topics(category: str) -> List[str]:
    client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
    db = client[os.getenv("MONGO_DB", "typology_bot")]
    collection = db["typology_entries"]
    return await collection.distinct("topic", {"category": category})

async def get_distinct_titles() -> List[str]:
    client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
    db = client[os.getenv("MONGO_DB", "typology_bot")]
    collection = db["typology_entries"]
    return await collection.distinct("title")

# ======================
# DATABASE INITIALIZATION
# ======================
async def initialize_database():
    client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
    db = client[os.getenv("MONGO_DB", "typology_bot")]
    
    await init_beanie(database=db, document_models=[TypologyEntry])
    
    if "typology_definitions" in await db.list_collection_names():
        old_collection = db["typology_definitions"]
        new_collection = db["typology_entries"]
        
        if await new_collection.count_documents({}) == 0:
            print("🚚 Migrating old definitions...")
            async for doc in old_collection.find():
                new_doc = {
                    "title": doc["term"],
                    "category": "General",
                    "topic": "General",
                    "description": doc["text"],
                    "author_id": doc["author_id"],
                    "author_name": doc["author_name"],
                    "created_at": doc["created_at"],
                    "last_updated": doc["last_updated"],
                    "image_url": doc.get("image_url", ""),
                    "reference": doc.get("reference", ""),
                    "votes": doc.get("votes", 0),
                    "voters": doc.get("voters", []),
                }
                await new_collection.insert_one(new_doc)
            
            await old_collection.rename("typology_definitions_backup")
    
    collection = db["typology_entries"]
    await collection.create_index([("title", "text")], name="title_text_idx", default_language="english")
    await collection.create_index([("category", 1)], name="category_idx")
    await collection.create_index([("topic", 1)], name="topic_idx")
    await collection.create_index([("votes", -1)], name="popularity_idx")

# ======================
# UI COMPONENTS
# ======================
class CategorySelect(ui.View):
    def __init__(self, categories: List[str]):
        super().__init__(timeout=60.0)
        self.category = None
        self.categories = categories
        self.add_item(CategoryDropdown(categories))

class CategoryDropdown(ui.Select):
    def __init__(self, categories: List[str]):
        options = [SelectOption(label=cat, value=cat, emoji="📂") for cat in categories]
        options.append(SelectOption(label="+ Create New Category", value="__new__", emoji="➕"))
        super().__init__(placeholder="Select a category...", options=options)
        
    async def callback(self, interaction: discord.Interaction):
        self.view.category = self.values[0]
        self.view.stop()
        await interaction.response.defer()

class TopicSelect(ui.View):
    def __init__(self, topics: List[str]):
        super().__init__(timeout=60.0)
        self.topic = None
        self.topics = topics
        self.add_item(TopicDropdown(topics))

class TopicDropdown(ui.Select):
    def __init__(self, topics: List[str]):
        options = [SelectOption(label=topic, value=topic, emoji="📝") for topic in topics]
        options.append(SelectOption(label="+ Create New Topic", value="__new__", emoji="➕"))
        super().__init__(placeholder="Select a topic...", options=options)
        
    async def callback(self, interaction: discord.Interaction):
        self.view.topic = self.values[0]
        self.view.stop()
        await interaction.response.defer()

# ======================
# ENTRY VIEW
# ======================
class EntryView(ui.View):
    def __init__(self, entries: List[TypologyEntry], user_id: int):
        super().__init__(timeout=120.0)
        self.entries = entries
        self.user_id = user_id
        self.page = 0
        self.message = None

    async def update_embed(self, interaction: Optional[discord.Interaction] = None):
        if not self.entries:
            if self.message:
                await self.message.edit(content="❌ No entries found", embed=None, view=None)
            return
            
        entry = self.entries[self.page]
        
        try:
            author = await bot.fetch_user(entry.author_id)
            author_info = author.display_name
            avatar = author.display_avatar.url
        except:
            author_info = "Unknown Author"
            avatar = None
        
        embed = discord.Embed(
            title=f"📚 {entry.title}",
            description=entry.description,
            color=0x6A0DAD,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="Category", value=f"```\n{entry.category}\n```", inline=False)
        embed.add_field(name="Topic", value=f"```\n{entry.topic}\n```", inline=False)
        
        if entry.reference:
            embed.add_field(name="Reference", value=f"```\n{entry.reference}\n```", inline=False)
            
        if entry.image_url:
            embed.set_image(url=entry.image_url)
            
        embed.set_author(name=author_info, icon_url=avatar)
        embed.set_footer(text=f"⭐ Votes: {entry.votes} • 🆔 ID: {entry.id} • 📅 Created: {discord.utils.format_dt(entry.created_at, style='R')}")
        
        if interaction:
            await interaction.response.edit_message(embed=embed, view=self)
        elif self.message:
            await self.message.edit(embed=embed, view=self)

    @ui.button(emoji="⬅️", style=discord.ButtonStyle.blurple)
    async def prev_page(self, interaction: discord.Interaction, button: ui.Button):
        self.page = max(0, self.page - 1)
        await self.update_embed(interaction)

    @ui.button(emoji="➡️", style=discord.ButtonStyle.blurple)
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        if self.page < len(self.entries) - 1:
            self.page += 1
            await self.update_embed(interaction)

    @ui.button(emoji="⭐", style=discord.ButtonStyle.green)
    async def upvote(self, interaction: discord.Interaction, button: ui.Button):
        entry = self.entries[self.page]
        
        if interaction.user.id in entry.voters:
            await interaction.response.send_message("❌ You've already voted!", ephemeral=True)
            return
            
        entry.voters.append(interaction.user.id)
        entry.votes += 1
        entry.last_updated = datetime.now(timezone.utc)
        await entry.save()
        
        await interaction.response.send_message("✅ Vote recorded!", ephemeral=True)
        await self.update_embed()

    @ui.button(emoji="✏️", style=discord.ButtonStyle.gray)
    async def edit_btn(self, interaction: discord.Interaction, button: ui.Button):
        entry = self.entries[self.page]
        
        if interaction.user.id != entry.author_id:
            await interaction.response.send_message("❌ You can only edit your own entries", ephemeral=True)
            return
            
        modal = EditModal(entry)
        await interaction.response.send_modal(modal)

    @ui.button(emoji="🚚", style=discord.ButtonStyle.secondary)
    async def move_btn(self, interaction: discord.Interaction, button: ui.Button):
        try:
            entry = self.entries[self.page]
            
            if interaction.user.id != entry.author_id:
                await interaction.response.send_message("❌ You can only move your own entries", ephemeral=True)
                return
                
            await interaction.response.defer(ephemeral=True)
            await self._execute_move_process(interaction, entry)
            
        except Exception as e:
            print(f"Move button error: {e}")
            await interaction.followup.send("❌ Failed to start move process", ephemeral=True)

    async def _execute_move_process(self, interaction: discord.Interaction, entry: TypologyEntry):
        """Complete move process with proper error handling"""
        try:
            # Step 1: Category Selection
            categories = await get_distinct_categories()
            if not categories:
                await interaction.followup.send("❌ No categories available", ephemeral=True)
                return

            category_view = CategorySelect(categories)
            category_msg = await interaction.followup.send(
                f"**📂 Select new category for '{entry.title}'**",
                view=category_view,
                ephemeral=True
            )
            
            await category_view.wait()
            if not category_view.category:
                await category_msg.edit(content="❌ Cancelled", view=None)
                return
                
            if category_view.category == "__new__":
                await category_msg.edit(content="⌛ Enter new category...", view=None)
                category = await self._get_text_input(interaction, "Enter category name:")
                if not category:
                    return
            else:
                category = category_view.category

            # Step 2: Topic Selection
            topics = await get_distinct_topics(category)
            topic_view = TopicSelect(topics)
            await category_msg.edit(
                content=f"**📝 Select topic in '{category}'**",
                view=topic_view
            )
            
            await topic_view.wait()
            if not topic_view.topic:
                await category_msg.edit(content="❌ Cancelled", view=None)
                return
                
            if topic_view.topic == "__new__":
                await category_msg.edit(content="⌛ Enter new topic...", view=None)
                topic = await self._get_text_input(interaction, "Enter topic name:")
                if not topic:
                    return
            else:
                topic = topic_view.topic

            # Update entry
            entry.category = category
            entry.topic = topic
            entry.last_updated = datetime.now(timezone.utc)
            await entry.save()
            
            await interaction.followup.send(f"✅ Moved to {category} → {topic}", ephemeral=True)
            await self.update_embed()
            
            try:
                await category_msg.delete()
            except:
                pass
                
        except Exception as e:
            print(f"Move process error: {e}")
            await interaction.followup.send("❌ Move failed", ephemeral=True)

    async def _get_text_input(self, interaction: discord.Interaction, prompt: str) -> Optional[str]:
        """Helper to get text input from user"""
        try:
            await interaction.followup.send(prompt, ephemeral=True)
            
            def check(m):
                return (
                    m.author == interaction.user 
                    and m.channel == interaction.channel
                    and not m.author.bot
                )
                
            response = await bot.wait_for(
                'message',
                timeout=60.0,
                check=check
            )
            content = response.content.strip()
            await response.delete()
            return content
            
        except asyncio.TimeoutError:
            await interaction.followup.send("⌛ Timed out", ephemeral=True)
            return None
        except Exception as e:
            print(f"Input error: {e}")
            await interaction.followup.send("❌ Invalid input", ephemeral=True)
            return None

    @ui.button(emoji="🗑️", style=discord.ButtonStyle.red)
    async def delete_btn(self, interaction: discord.Interaction, button: ui.Button):
        entry = self.entries[self.page]
        
        MOD_ROLE_ID = int(os.getenv("MOD_ROLE_ID", 0))
        is_author = interaction.user.id == entry.author_id
        is_mod = MOD_ROLE_ID and any(role.id == MOD_ROLE_ID for role in interaction.user.roles)
        
        if not (is_author or is_mod):
            await interaction.response.send_message("❌ You can only delete your own entries", ephemeral=True)
            return
            
        await entry.delete()
        self.entries.pop(self.page)
        
        if not self.entries:
            await interaction.response.edit_message(content="✅ Entry deleted", embed=None, view=None)
            return
            
        if self.page >= len(self.entries):
            self.page = max(0, len(self.entries) - 1)
            
        await interaction.response.send_message("✅ Entry deleted", ephemeral=True)
        await self.update_embed()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            await self.message.edit(view=self)

# ======================
# EDIT MODAL
# ======================
class EditModal(ui.Modal, title="Edit Entry Content"):
    new_description = ui.TextInput(label="Description", style=discord.TextStyle.long, required=True)
    new_image = ui.TextInput(label="Image URL", style=discord.TextStyle.short, required=False)
    new_reference = ui.TextInput(label="Reference", style=discord.TextStyle.short, required=False)
    
    def __init__(self, entry: TypologyEntry):
        super().__init__()
        self.entry = entry
        self.new_description.default = entry.description
        self.new_image.default = entry.image_url
        self.new_reference.default = entry.reference
    
    async def on_submit(self, interaction: discord.Interaction):
        self.entry.description = str(self.new_description)
        self.entry.image_url = str(self.new_image)
        self.entry.reference = str(self.new_reference)
        self.entry.last_updated = datetime.now(timezone.utc)
        await self.entry.save()
        
        if self.entry.image_url:
            preview_embed = discord.Embed(title="Image Preview", color=0x3498DB)
            preview_embed.set_image(url=self.entry.image_url)
            await interaction.response.send_message(
                "✅ Content updated! Here's your image preview:",
                embed=preview_embed,
                ephemeral=True
            )
        else:
            await interaction.response.send_message("✅ Content updated!", ephemeral=True)

# ======================
# DEFINITION CREATION FLOW
# ======================
async def create_definition_flow(message: discord.Message):
    try:
        # Extract title (case-insensitive)
        title = re.sub(r'^tp define\s*', '', message.content, flags=re.IGNORECASE).strip()
        if not title:
            await message.channel.send("❌ Please provide a title after `tp define`")
            return
            
        # Check for existing title
        existing = await TypologyEntry.find_one({
            "title": {"$regex": f"^{re.escape(title)}$", "$options": "i"}
        })
        if existing:
            await message.channel.send(f"⚠️ An entry with title '{title}' already exists")
            return
        
        # Category selection
        categories = await get_distinct_categories()
        category_view = CategorySelect(categories)
        category_msg = await message.channel.send(f"**📂 Select a category for '{title}'**", view=category_view)
        
        await category_view.wait()
        if not category_view.category:
            await category_msg.edit(content="❌ Category selection cancelled", view=None)
            return
            
        if category_view.category == "__new__":
            await category_msg.edit(content="⌛ Waiting for new category...", view=None)
            
            def check(m):
                return m.author == message.author and m.channel == message.channel
                
            try:
                await message.channel.send("Please enter a name for the new category:")
                response = await bot.wait_for('message', timeout=60.0, check=check)
                category = response.content.strip()
                await response.delete()
            except asyncio.TimeoutError:
                await message.channel.send("⌛ Category creation timed out")
                return
        else:
            category = category_view.category
        
        # Topic selection
        topics = await get_distinct_topics(category)
        topic_view = TopicSelect(topics)
        await category_msg.edit(content=f"**📝 Select a topic in '{category}'**", view=topic_view)
        
        await topic_view.wait()
        if not topic_view.topic:
            await category_msg.edit(content="❌ Topic selection cancelled", view=None)
            return
            
        if topic_view.topic == "__new__":
            await category_msg.edit(content="⌛ Waiting for new topic...", view=None)
            
            try:
                await message.channel.send("Please enter a name for the new topic:")
                response = await bot.wait_for('message', timeout=60.0, check=check)
                topic = response.content.strip()
                await response.delete()
            except asyncio.TimeoutError:
                await message.channel.send("⌛ Topic creation timed out")
                return
        else:
            topic = topic_view.topic
        
        # Get content from replied message
        if not message.reference:
            await message.channel.send("❌ Please reply to a message with the content")
            return
            
        try:
            content_msg = await message.channel.fetch_message(message.reference.message_id)
            if content_msg.author.bot:
                await message.channel.send("❌ Cannot use bot messages as content")
                return
                
            description = content_msg.content
        except:
            await message.channel.send("❌ Failed to fetch content message")
            return
            
        # Create entry
        entry = TypologyEntry(
            title=title,
            category=category,
            topic=topic,
            description=description,
            author_id=message.author.id,
            author_name=message.author.display_name
        )
        await entry.insert()
        
        # Send confirmation
        embed = discord.Embed(
            title=f"✅ Entry created: {title}",
            description=f"**{category} → {topic}**\n{description[:200]}...",
            color=0x00ff00
        )
        embed.add_field(name="Full Content", value=f"[Jump to Message]({content_msg.jump_url})")
        await message.channel.send(embed=embed)
        await category_msg.delete()
        
    except Exception as e:
        await message.channel.send(f"❌ Error in creation flow: {str(e)}")

# ======================
# BOT SETUP
# ======================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"🔄 Initializing database...")
    try:
        await initialize_database()
        print(f"✅ Bot ready as {bot.user}")
    except Exception as e:
        print(f"❌ Database init failed: {e}")

# ======================
# MESSAGE HANDLING
# ======================
@bot.event
async def on_message(message):
    await bot.process_commands(message)
    
    if re.match(r'^tp define\b', message.content, re.IGNORECASE) and not message.author.bot:
        await create_definition_flow(message)

# ======================
# COMMANDS
# ======================
@bot.command()
async def define(ctx, *, title: str = None):
    """Browse entries through a hierarchical interface"""
    try:
        if not title:
            # Hierarchical browsing
            categories = await get_distinct_categories()
            if not categories:
                await ctx.send("❌ No categories found")
                return
                
            view = CategorySelect(categories)
            msg = await ctx.send("📂 **Select a category:**", view=view)
            await view.wait()
            
            if not view.category or view.category == "__new__":
                await msg.edit(content="❌ Category selection cancelled", view=None)
                return
                
            topics = await get_distinct_topics(view.category)
            if not topics:
                await msg.edit(content=f"❌ No topics found for {view.category}", view=None)
                return
                
            topic_view = TopicSelect(topics)
            await msg.edit(content=f"📝 **Select a topic in {view.category}:**", view=topic_view)
            await topic_view.wait()
            
            if not topic_view.topic or topic_view.topic == "__new__":
                await msg.edit(content="❌ Topic selection cancelled", view=None)
                return
                
            entries = await TypologyEntry.find(
                TypologyEntry.category == view.category,
                TypologyEntry.topic == topic_view.topic
            ).sort(-TypologyEntry.votes).to_list()
            
            if not entries:
                await msg.edit(content=f"❌ No entries found for {view.category}/{topic_view.topic}", view=None)
                return
                
            entry_view = EntryView(entries, ctx.author.id)
            entry_view.message = await ctx.send(
                embed=discord.Embed(
                    title=f"Loading entries for {topic_view.topic}...",
                    color=0x6A0DAD
                ),
                view=entry_view
            )
            await entry_view.update_embed()
            await msg.delete()
            
        else:
            # Direct title search
            entries = await TypologyEntry.find({
                "title": {"$regex": f"^{re.escape(title)}$", "$options": "i"}
            }).sort(-TypologyEntry.votes).to_list()
            
            if not entries:
                await ctx.send(f"❌ No entries found for '{title}'")
                return
                
            view = EntryView(entries, ctx.author.id)
            view.message = await ctx.send(
                embed=discord.Embed(
                    title=f"Loading entries for '{title}'...",
                    color=0x6A0DAD
                ),
                view=view
            )
            await view.update_embed()
            
    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")

@bot.command()
async def search(ctx, *, query: str):
    """Search across all entries"""
    try:
        results = await TypologyEntry.find(
            {"$text": {"$search": query}}
        ).sort(-TypologyEntry.votes).to_list()
        
        if not results:
            await ctx.send("🔍 No results found")
            return
            
        view = EntryView(results, ctx.author.id)
        view.message = await ctx.send(
            embed=discord.Embed(
                title=f"Search results for: {query}",
                color=0x3498DB
            ),
            view=view
        )
        await view.update_embed()
    except Exception as e:
        await ctx.send(f"❌ Search error: {str(e)}")

# ======================
# START BOT
# ======================
if __name__ == "__main__":
    required_vars = ["DISCORD_TOKEN", "MONGO_URI"]
    if missing := [var for var in required_vars if not os.getenv(var)]:
        print(f"❌ Missing environment variables: {', '.join(missing)}")
        exit(1)
    
    if not os.getenv("MOD_ROLE_ID"):
        print("⚠️ MOD_ROLE_ID not set - moderator deletion disabled")
    
    try:
        bot.run(os.getenv("DISCORD_TOKEN"))
    except Exception as e:
        print(f"💥 Bot crashed: {str(e)}")